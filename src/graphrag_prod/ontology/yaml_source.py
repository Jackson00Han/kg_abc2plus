"""Bounded, non-executing ontology authoring-file diagnostics.

The original UTF-8 text and its byte checksum are returned separately from the
canonical semantic JSON. YAML comments and formatting are never evidence-lost
by mistaking the normalized checksum for the upload checksum.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

import yaml

from .source import MAX_SOURCE_BYTES, canonical_json, compile_source_contract

MAX_DEPTH = 32
MAX_NODES = 30_000
MAX_SCALAR_CHARS = 65_536


class OntologySourceError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_contract", path: str = "$", line: int | None = None, column: int | None = None) -> None:
        super().__init__(message)
        self.code, self.path, self.line, self.column = code, path, line, column

    def diagnostic(self) -> dict[str, Any]:
        return {"severity": "error", "code": self.code, "message": str(self), "path": self.path, "line": self.line, "column": self.column}


class _JsonScalarLoader(yaml.SafeLoader):
    """YAML collections with predictable JSON-compatible scalar semantics."""
    yaml_implicit_resolvers: dict[str, Any] = {}


_JsonScalarLoader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$", re.I), list("tTfF"))
_JsonScalarLoader.add_implicit_resolver("tag:yaml.org,2002:null", re.compile(r"^(?:null|~|)$", re.I), ["n", "N", "~", ""])
_JsonScalarLoader.add_implicit_resolver("tag:yaml.org,2002:int", re.compile(r"^-?(?:0|[1-9][0-9]*)$"), list("-0123456789"))
_JsonScalarLoader.add_implicit_resolver("tag:yaml.org,2002:float", re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+(?:[eE][+-]?[0-9]+)?|[eE][+-]?[0-9]+)$"), list("-0123456789"))


@dataclass(frozen=True, slots=True)
class ParsedOntologySource:
    source: dict[str, Any]
    source_text: str
    source_sha256: str
    locations: dict[str, tuple[int, int]]


def parse_ontology_source(raw: bytes | str) -> ParsedOntologySource:
    """Reject duplicate keys, aliases, tags, multi-documents and deep inputs."""
    if not isinstance(raw, (bytes, str)):
        raise OntologySourceError("本体文件必须是 UTF-8 文本。", code="invalid_encoding")
    try:
        data = raw if isinstance(raw, bytes) else raw.encode("utf-8")
        if len(data) > MAX_SOURCE_BYTES:
            raise OntologySourceError(f"本体文件不能超过 {MAX_SOURCE_BYTES} 字节。", code="size_limit")
        text = data.decode("utf-8-sig")
    except UnicodeError as exc:
        raise OntologySourceError("本体文件必须使用 UTF-8 编码。", code="invalid_encoding") from exc
    if not text.strip():
        raise OntologySourceError("本体文件为空。", code="empty_document")
    locations: dict[str, tuple[int, int]] = {}
    try:
        depth = count = documents = 0
        for event in yaml.parse(text, Loader=_JsonScalarLoader):
            mark = event.start_mark
            position = {"line": mark.line + 1, "column": mark.column + 1}
            count += 1
            if count > MAX_NODES * 3:
                raise OntologySourceError("本体声明数量超过解析预算。", code="node_limit", **position)
            if isinstance(event, yaml.events.DocumentStartEvent):
                documents += 1
                if documents > 1:
                    raise OntologySourceError("一次只能上传一个 YAML 文档。", code="multiple_documents", **position)
            if isinstance(event, yaml.events.AliasEvent) or getattr(event, "anchor", None):
                raise OntologySourceError("本体文件不接受 YAML 锚点或别名，请展开声明。", code="yaml_alias", **position)
            if getattr(event, "tag", None):
                raise OntologySourceError("本体文件不接受显式 YAML 标签。", code="yaml_tag", **position)
            if isinstance(event, (yaml.events.MappingStartEvent, yaml.events.SequenceStartEvent)):
                depth += 1
                if depth > MAX_DEPTH:
                    raise OntologySourceError("本体嵌套层数超过解析预算。", code="depth_limit", **position)
            if isinstance(event, (yaml.events.MappingEndEvent, yaml.events.SequenceEndEvent)):
                depth -= 1
            if isinstance(event, yaml.events.ScalarEvent) and len(event.value) > MAX_SCALAR_CHARS:
                raise OntologySourceError("单个本体字段超过长度预算。", code="scalar_limit", **position)
        root = yaml.compose(text, Loader=_JsonScalarLoader)
        if root is None:
            raise OntologySourceError("本体文件不包含任何声明。", code="empty_document", line=1, column=1)
        nodes = 0

        def convert(node: yaml.Node, path: str) -> Any:
            nonlocal nodes
            nodes += 1
            mark = node.start_mark
            position = {"line": mark.line + 1, "column": mark.column + 1}
            locations[path] = (position["line"], position["column"])
            if nodes > MAX_NODES:
                raise OntologySourceError("本体声明数量超过解析预算。", code="node_limit", path=path, **position)
            if isinstance(node, yaml.MappingNode):
                result: dict[str, Any] = {}
                for key, value in node.value:
                    if not isinstance(key, yaml.ScalarNode) or key.tag != "tag:yaml.org,2002:str":
                        raise OntologySourceError("JSON 对象的字段名必须是文字。", code="invalid_key", path=path, line=key.start_mark.line + 1, column=key.start_mark.column + 1)
                    child_path = f"{path}.{key.value}"
                    if key.value in result:
                        raise OntologySourceError("同一对象包含重复字段。", code="duplicate_key", path=child_path, line=key.start_mark.line + 1, column=key.start_mark.column + 1)
                    if key.value == "<<":
                        raise OntologySourceError("请展开 YAML 合并声明。", code="yaml_merge", path=child_path, **position)
                    result[key.value] = convert(value, child_path)
                return result
            if isinstance(node, yaml.SequenceNode):
                return [convert(item, f"{path}[{index}]") for index, item in enumerate(node.value)]
            tag = node.tag.rsplit(":", 1)[-1]
            if tag == "str":
                return node.value
            if tag == "null":
                return None
            if tag == "bool":
                return node.value.lower() == "true"
            if tag == "int":
                return int(node.value)
            if tag == "float":
                return float(node.value)
            raise OntologySourceError("字段不是支持的 JSON 值类型。", code="invalid_scalar", path=path, **position)

        source = convert(root, "$")
        if not isinstance(source, dict):
            raise OntologySourceError("本体顶层必须是对象。", code="invalid_root", line=1, column=1)
        canonical_json(source)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        raise OntologySourceError("YAML 语法无效，请检查缩进、引号和括号。", code="yaml_syntax", line=None if mark is None else mark.line + 1, column=None if mark is None else mark.column + 1) from exc
    except (RecursionError, OverflowError, ValueError) as exc:
        if isinstance(exc, OntologySourceError):
            raise
        raise OntologySourceError("本体含有超出 JSON 范围的值。", code="invalid_scalar") from exc
    return ParsedOntologySource(source, data.decode("utf-8"), hashlib.sha256(data).hexdigest(), locations)


def diagnose_ontology_source(raw: bytes | str, *, tenant_id: str, filename: str = "ontology.yaml") -> dict[str, Any]:
    """Return a reviewable draft only; this function performs no persistence."""
    result: dict[str, Any] = {"valid": False, "diagnostics": [], "normalized_tbox": None, "source_json": None, "source_text": None, "source_sha256": None, "filename": filename, "format": "json" if filename.lower().endswith(".json") else "yaml", "profile": None, "source_version": None}
    parsed = None
    source_path = "$"
    try:
        parsed = parse_ontology_source(raw)
        result.update(source_text=parsed.source_text, source_sha256=parsed.source_sha256, source_json=canonical_json(parsed.source))
        source = dict(parsed.source)
        wrapper_checksum = None
        if "schema" in source:
            if source["schema"] != "graphrag-property-tbox-export-v1" or set(source) - {"schema", "exported_tbox_id", "status", "checksum", "definition"} or not isinstance(source.get("definition"), dict):
                raise OntologySourceError("不支持的本体 JSON 导出封装。", path="$.schema")
            wrapper_checksum = source.get("checksum")
            source = dict(source["definition"])
            source_path = "$.definition"
        expected_checksum = source.pop("expected_checksum", wrapper_checksum)
        if expected_checksum is not None and (not isinstance(expected_checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_checksum)):
            raise OntologySourceError("expected_checksum 必须是小写十六进制 SHA-256。", path="$.expected_checksum")
        if wrapper_checksum is not None and wrapper_checksum != expected_checksum:
            raise OntologySourceError("导出封装与定义中的预期校验和不一致。", path="$.expected_checksum")
        result["expected_checksum"] = expected_checksum
        from .source import validate_rule_reference_registry  # noqa: PLC0415
        registry = source.pop("rule_reference_registry", None)
        registry_json = source.pop("rule_reference_registry_json", None)
        if registry is not None:
            if registry_json is not None:
                raise OntologySourceError("规则引用目录只能使用一种表示方式。", path="$.rule_reference_registry")
            registry_json = canonical_json(validate_rule_reference_registry(registry))
        if registry_json is not None:
            if not isinstance(registry_json, str):
                raise OntologySourceError("规则引用目录 JSON 必须是文字。", path="$.rule_reference_registry_json")
            registry_json = canonical_json(validate_rule_reference_registry(json.loads(registry_json)))
        result["source_json"] = canonical_json(source)
        from .models import TBoxVersion  # noqa: PLC0415
        if "metadata" in source:
            compiled = compile_source_contract(source)
        else:
            compiled = source
            if "tenant_id" in compiled and compiled["tenant_id"] != tenant_id:
                raise OntologySourceError("导入文件中的租户与当前认证租户不一致。", code="tenant_mismatch", path="$.tenant_id")
        if registry_json is not None:
            compiled["rule_reference_registry_json"] = registry_json
        tbox = TBoxVersion.from_mapping({**compiled, "tenant_id": tenant_id, "status": "DRAFT"})
        from .source import source_import_report  # noqa: PLC0415
        capabilities = source_import_report(tbox.source_contract_json) or {"profile": "native_tbox_json_v1", "source_version": str(tbox.version), "blocked_entity_types": [item.name for item in tbox.entity_types if not item.instance_allowed], "blocked_relationship_types": [item.name for item in tbox.relationship_types if not item.instance_allowed], "scope": "兼容已有本体 JSON；校验后作为草稿保存，不更改审核或发布状态。"}
        result.update(valid=True, normalized_tbox=tbox.to_mapping(include_computed=True), capabilities=capabilities, profile=capabilities["profile"], source_version=capabilities["source_version"])
        result["diagnostics"] = [{"severity": "info", "code": "validated", "message": "本体结构与声明约束校验通过；规范 JSON 将作为草稿保存。来源等级、业务状态与平台审核发布状态分别保留。", "path": "$", "line": 1, "column": 1}]
        if source.get("metadata", {}).get("model_kind") == "property_graph":
            path = source_path + ".constraints.entity_identity"
            result["diagnostics"].append({"severity": "info", "code": "identity_mapping", "message": "源本体的实体编号声明映射到平台稳定实体 ID；来源中的编号仍须经身份映射，不按同名或工程短编号自动合并。", "path": path, "line": parsed.locations.get(path, (None, None))[0], "column": parsed.locations.get(path, (None, None))[1]})
    except ValueError as exc:
        issue = exc if isinstance(exc, OntologySourceError) else OntologySourceError(str(exc))
        if source_path != "$" and not issue.path.startswith(source_path):
            issue.path = source_path + issue.path[1:]
        if parsed and issue.line is None:
            issue.line, issue.column = parsed.locations.get(issue.path, parsed.locations.get("$", (None, None)))
        result["diagnostics"] = [issue.diagnostic()]
    except (TypeError, KeyError, IndexError, AttributeError):
        # Native portable JSON uses the existing T-Box constructors. Their
        # malformed nested values can raise these structural exceptions before
        # a ValueError is produced; expose no raw input or Python tracebacks.
        issue = OntologySourceError("本体声明的对象、列表或字段类型不符合规范。", code="invalid_structure", path=source_path)
        if parsed:
            issue.line, issue.column = parsed.locations.get(source_path, (None, None))
        result["diagnostics"] = [issue.diagnostic()]
    return result
