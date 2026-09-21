"""Declarative XML preflight with source spans and explicit structural proof.

This produces an audit preview, never A-Box records or published knowledge.
Selectors and finite conversions come from a versioned mapping; equipment
names, source codes and sample counts do not belong in this executor.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any

from graphrag_prod.ontology.models import TBoxVersion
from .literals import LiteralNormalizationError, TBoxLiteralNormalizer
from .parser import DocumentParseError
from .xml_parser import XmlElement, XmlScalar, locate_xml

VERSION = "xml-mapping-preflight:v1"
MAPPING_VERSION = "xml-declarative-mapping:v1"
MAX_RECORDS = 2_000
MAX_FIELDS = 30_000
MAX_DIAGNOSTICS = 1_000
MAX_RELATIONSHIPS = 8_000
MAX_REPORT_CHARS = 4_000_000
MAX_ANCHOR_CHARS = 16_384
_NAME = r"[A-Za-z_][A-Za-z0-9_.:-]*"
_STEP = re.compile(rf"^({_NAME})(?:\[@({_NAME})='([^'\[\]<>]{{0,512}})'\])?$")
_NUMBER_UNIT = re.compile(r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s+([^\s]+)$")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _shape(value: Any, required: set[str], optional: set[str] = frozenset()) -> None:
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - optional:
        raise ValueError("unexpected or missing mapping fields")


def _rows(value: Any, maximum: int) -> list:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError("mapping list exceeds its limit or is not an array")
    return value


@dataclass(frozen=True)
class _Selection:
    node: XmlElement
    path: str
    scalar: XmlScalar | None = None


class _Preview:
    def __init__(self, text: str, mapping: dict, tbox: TBoxVersion, checksum: str | None):
        self.text, self.mapping, self.tbox = text, mapping, tbox
        normalized_checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if checksum is not None and re.fullmatch(r"[0-9a-f]{64}", checksum) is None:
            raise ValueError("source_checksum must be SHA-256")
        self.result = {
            "version": VERSION, "valid": False, "candidate_only": True,
            "publication_status": "not_published", "source_checksum": checksum or normalized_checksum,
            "normalized_checksum": normalized_checksum, "mapping_checksum": _digest(mapping),
            "ontology_checksum": tbox.checksum, "entities": [], "relationships": [],
            "unresolved_references": [], "coverage": {}, "semantic_context": {}, "diagnostics": [],
        }
        self.root = locate_xml(text)
        self.nodes = tuple(self.root.walk())
        self.parents = {child.path: parent for parent in self.nodes for child in parent.children}
        self.inventory: dict[str, _Selection] = {}
        self.fields_by_node: dict[str, list[_Selection]] = {}
        for node in self.nodes:
            for name, scalar in node.attributes.items():
                self.inventory[node.path + "/@" + name] = _Selection(node, node.path + "/@" + name, scalar)
            for index, scalar in enumerate(node.text):
                if scalar.value.strip():
                    path = node.path + f"/text()[{index + 1}]"
                    self.inventory[path] = _Selection(node, path, scalar)
        if len(self.inventory) > MAX_FIELDS:
            raise ValueError("XML scalar field budget exceeded")
        for field in self.inventory.values():
            self.fields_by_node.setdefault(field.node.path, []).append(field)
        self.dispositions: dict[str, tuple[str, str]] = {}
        self.types = {item.name: item for item in tbox.entity_types if item.instance_allowed}
        self.relations = {item.name: item for item in tbox.relationship_types if item.instance_allowed}
        self.groups: dict[str, dict[str, dict]] = {}
        self.normalizer = TBoxLiteralNormalizer()
        self.anchor_chars = 0
        self.output_chars = 0

    def diagnostic(self, code: str, message: str, path: str = "$", severity: str = "error") -> None:
        if len(self.result["diagnostics"]) >= MAX_DIAGNOSTICS:
            raise ValueError("XML diagnostic budget exceeded")
        self.result["diagnostics"].append({"severity": severity, "code": code, "path": path, "message": message})

    def anchor(self, value: _Selection | XmlElement) -> dict:
        if isinstance(value, XmlElement):
            start, end, path = value.start, value.start_tag_end, value.path
        elif value.scalar is None:
            return self.anchor(value.node)
        else:
            start, end, path = value.scalar.start, value.scalar.end, value.path
        if end - start > MAX_ANCHOR_CHARS:
            raise ValueError("XML evidence anchor exceeds its character budget")
        self.anchor_chars += end - start
        if self.anchor_chars > MAX_REPORT_CHARS:
            raise ValueError("XML cumulative evidence text exceeds its output budget")
        return {"path": path, "char_start": start, "char_end": end, "text": self.text[start:end]}

    def emit(self, bucket: str, item: dict) -> None:
        self.output_chars += len(_canonical(item))
        if self.output_chars > MAX_REPORT_CHARS:
            raise ValueError("XML preview report exceeds its output budget")
        self.result[bucket].append(item)

    def mark(self, selection: _Selection, disposition: str, reason: str) -> None:
        if selection.scalar is not None:
            if selection.path.endswith("/text()"):
                for field in self.fields_by_node.get(selection.node.path, []):
                    if field.node.path == selection.node.path and field.path.startswith(selection.path + "[") and (
                        selection.scalar.start <= field.scalar.start <= field.scalar.end <= selection.scalar.end
                    ):
                        self.mark(field, disposition, reason)
                return
            previous = self.dispositions.get(selection.path)
            if previous is None or previous[0] in {"retained", "binding"}:
                self.dispositions[selection.path] = (disposition, reason)

    def select(self, source: XmlElement, selector: str, *, bind: bool = True) -> list[_Selection]:
        """Small path language: names, /, //, @attribute, and exact predicates.

        No XPath functions (except terminal text()), parent traversal, code,
        wildcard element walks, external documents, or evaluation is supported.
        """
        if not isinstance(selector, str) or not 0 < len(selector) <= 2_048:
            raise ValueError("selector must be a bounded nonempty string")
        if selector.startswith("//") or selector.endswith("/") or "///" in selector:
            raise ValueError("unsupported XML selector")
        absolute = selector.startswith("/")
        pieces = re.split(r"(/+)", selector.lstrip("/"))
        current = [self.root] if absolute else [source]
        axis, first = "/", True
        for index in range(0, len(pieces), 2):
            step = pieces[index]
            if index:
                axis = pieces[index - 1]
            last = index == len(pieces) - 1
            if step.startswith("@") or step == "text()":
                if not last or axis != "/":
                    raise ValueError("attribute/text selector must be a direct terminal step")
                result = []
                for node in current:
                    if step == "text()":
                        # Expat can split a single value at an entity reference,
                        # including a whitespace reference such as ``A&#32;B``.
                        # Keep every fragment of a meaningful text value so its
                        # decoded value and contiguous original span agree.
                        # Pure indentation around child elements is not a value.
                        if any(scalar.value.strip() for scalar in node.text):
                            result.extend(_Selection(node, node.path + f"/text()[{i + 1}]", scalar)
                                          for i, scalar in enumerate(node.text))
                    else:
                        attribute = step[1:]
                        if attribute != "*" and re.fullmatch(_NAME, attribute) is None:
                            raise ValueError("invalid XML attribute selector")
                        result.extend(_Selection(node, node.path + "/@" + name, scalar)
                                      for name, scalar in node.attributes.items() if attribute in {"*", name})
                return result
            match = _STEP.fullmatch(step)
            if match is None or axis not in {"/", "//"}:
                raise ValueError("unsupported XML selector step")
            name, predicate_name, predicate_value = match.groups()
            candidates = current if absolute and first else [
                child for node in current for child in
                (tuple(node.walk())[1:] if axis == "//" else node.children)
            ]
            selected = {}
            for node in candidates:
                if node.name != name or node.namespace_uri != self.root.namespace_uri:
                    continue
                if predicate_name is not None:
                    scalar = node.attributes.get(predicate_name)
                    if scalar is None or scalar.value != predicate_value:
                        continue
                    if bind:
                        self.mark(_Selection(node, node.path + "/@" + predicate_name, scalar),
                                  "binding", "selector predicate")
                selected[node.path] = node
            current, first = list(selected.values()), False
        return [_Selection(node, node.path) for node in current]

    def scalar(self, node: XmlElement, selector: str, *, required: bool = False) -> _Selection | None:
        selected = self.select(node, selector)
        if not selected and not required:
            return None
        # Entity references may split one contiguous text value into callbacks.
        # Preserve its original spelling; never flatten intervening markup.
        if selector.endswith("text()") and len(selected) > 1 and all(
            left.node.path == right.node.path and left.scalar.end == right.scalar.start
            for left, right in zip(selected, selected[1:])
        ):
            value = XmlScalar("".join(item.scalar.value for item in selected),
                              selected[0].scalar.start, selected[-1].scalar.end)
            return _Selection(selected[0].node, selected[0].node.path + "/text()", value)
        if len(selected) != 1 or selected[0].scalar is None:
            raise ValueError("selector must resolve to exactly one scalar: " + selector)
        return selected[0]

    def retain(self, node: XmlElement, rules: list) -> None:
        for rule in _rows(rules, 100):
            _shape(rule, {"select", "reason"}, {"descendants"})
            reason = rule["reason"]
            if not isinstance(reason, str) or not 0 < len(reason.strip()) <= 512:
                raise ValueError("retained fields require a bounded reason")
            if "descendants" in rule and not isinstance(rule["descendants"], bool):
                raise ValueError("descendants must be boolean")
            for selected in self.select(node, rule["select"]):
                if selected.scalar:
                    self.mark(selected, "retained", reason)
                else:
                    nodes = selected.node.walk() if rule.get("descendants") else (selected.node,)
                    for element in nodes:
                        for field in self.fields_by_node.get(element.path, []):
                            self.mark(field, "retained", reason)

    def nearest(self, node: XmlElement, collection: str) -> dict | None:
        parent = self.parents.get(node.path)
        while parent is not None:
            if parent.path in self.groups.get(collection, {}):
                return self.groups[collection][parent.path]
            parent = self.parents.get(parent.path)
        return None

    def entity_type(self, node: XmlElement, rule: dict) -> tuple[str | None, list]:
        declaration = rule["type"]
        if isinstance(declaration, str):
            return declaration, []
        _shape(declaration, {"cases"})
        matches, anchors = [], []
        for case in _rows(declaration["cases"], 64):
            _shape(case, {"when", "entity_type"})
            if not isinstance(case["when"], dict) or not 1 <= len(case["when"]) <= 8:
                raise ValueError("type cases require bounded source predicates")
            selected = [self.scalar(node, selector) for selector in case["when"]]
            for scalar in selected:
                if scalar:
                    self.mark(scalar, "binding", "entity type discriminator")
            if all(item is not None and item.scalar.value == expected
                   for item, expected in zip(selected, case["when"].values())):
                matches.append(case["entity_type"])
                anchors.extend(self.anchor(item) for item in selected)
        if len(matches) != 1:
            self.diagnostic("XML_TYPE_UNMAPPED" if not matches else "XML_TYPE_AMBIGUOUS",
                            "source record requires exactly one configured ontology type", node.path)
            return None, anchors
        return matches[0], anchors

    def property(self, node: XmlElement, kind: str, rule: dict, index: int) -> dict | None:
        _shape(rule, {"select", "property"}, {"transform", "values", "source_unit", "types"})
        selected = self.scalar(node, rule["select"])
        if selected is None or not selected.scalar.value:
            return None
        definitions = {item.name: item for item in self.types[kind].properties}
        if rule.get("types") is not None and kind not in _rows(rule["types"], 64):
            self.mark(selected, "retained", "property mapping does not apply to this ontology type")
            return None
        definition = definitions.get(rule["property"])
        if definition is None:
            self.diagnostic("XML_PROPERTY_UNDECLARED", "property is not declared on the selected ontology type", selected.path)
            return None
        transform = rule.get("transform", "copy")
        value, unit = selected.scalar.value, None
        if transform == "enum":
            values = rule.get("values")
            if not isinstance(values, dict) or not 0 < len(values) <= 64 or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in values.items()
            ):
                raise ValueError("enum conversion requires a finite string map")
            if value not in values:
                self.diagnostic("XML_ENUM_UNMAPPED", "source code has no configured semantic mapping", selected.path)
                return None
            value = values[value]
        elif transform == "quantity":
            matched = _NUMBER_UNIT.fullmatch(value)
            if matched is None or not isinstance(rule.get("source_unit"), str) or matched.group(2) != rule["source_unit"]:
                self.diagnostic("XML_QUANTITY_INVALID", "quantity must contain an explicit configured source unit", selected.path)
                return None
            value, unit = matched.groups()
        elif transform != "copy":
            raise ValueError("unsupported XML property transform")
        try:
            literal = self.normalizer.normalize(definition, raw_value=value, raw_unit=unit,
                                               valid_from=None, valid_to=None, observed_at=None)
        except LiteralNormalizationError as error:
            self.diagnostic(error.code, error.detail, selected.path)
            return None
        self.mark(selected, "mapped", "ontology property " + rule["property"])
        return {"property": rule["property"], "value": literal.typed_value,
                "canonical_value": literal.canonical_value, "canonical_unit": literal.canonical_unit,
                "raw_value": selected.scalar.value, "transform": transform,
                "mapping_rule_index": index, "evidence": self.anchor(selected),
                "structural_anchors": [self.anchor(node)]}

    def records(self) -> None:
        for rule in _rows(self.mapping["collections"], 16):
            _shape(rule, {"id", "select", "type", "identity", "properties", "retained_fields"})
            collection = rule["id"]
            if not isinstance(collection, str) or re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", collection) is None or collection in self.groups:
                raise ValueError("collection ID must be unique and bounded")
            self.groups[collection] = {}
            selected = {}
            for selector in _rows(rule["select"], 16):
                for selection in self.select(self.root, selector):
                    if selection.scalar:
                        raise ValueError("record selectors must select XML elements")
                    selected[selection.path] = selection.node
            identity = rule["identity"]
            _shape(identity, {"fields"}, {"scope", "owner_collection"})
            identities = set()
            for node in sorted(selected.values(), key=lambda item: item.start):
                if sum(len(group) for group in self.groups.values()) >= MAX_RECORDS:
                    raise ValueError("XML record budget exceeded")
                self.retain(node, rule["retained_fields"])
                kind, type_anchors = self.entity_type(node, rule)
                if kind is None:
                    continue
                if kind not in self.types:
                    self.diagnostic("XML_TYPE_UNDECLARED", "mapped type is not instantiable in the active ontology", node.path)
                    continue
                fields, identity_anchors = [], []
                if "owner_collection" in identity:
                    owner = self.nearest(node, identity["owner_collection"])
                    if owner is None:
                        self.diagnostic("XML_IDENTITY_OWNER_MISSING", "record has no uniquely mapped ancestor owner", node.path)
                        continue
                    fields.append(owner["source_identity"])
                    identity_anchors.extend(owner["identity_anchors"])
                identity_failed = False
                for selector in [*_rows(identity.get("scope", []), 8), *_rows(identity["fields"], 8)]:
                    try:
                        scalar = self.scalar(node, selector, required=True)
                        assert scalar is not None and scalar.scalar is not None
                        if not scalar.scalar.value.strip() or len(scalar.scalar.value) > 512:
                            raise ValueError("identity value is empty or too long")
                    except ValueError as error:
                        self.diagnostic("XML_IDENTITY_INVALID", str(error), node.path)
                        identity_failed = True
                        break
                    fields.append(scalar.scalar.value)
                    identity_anchors.append(self.anchor(scalar))
                    self.mark(scalar, "binding", "stable source identity")
                if identity_failed or not fields:
                    continue
                source_identity = "xml:" + _digest({"collection": collection, "parts": fields})
                if source_identity in identities:
                    self.diagnostic("XML_IDENTITY_DUPLICATE", "duplicate source identity requires review", node.path)
                    # Neither occurrence may silently win a later reference join.
                    for path, existing in tuple(self.groups[collection].items()):
                        if existing["source_identity"] == source_identity:
                            del self.groups[collection][path]
                    self.result["entities"] = [item for item in self.result["entities"]
                                               if item["source_identity"] != source_identity]
                    continue
                identities.add(source_identity)
                properties = [item for index, prop in enumerate(_rows(rule["properties"], 100))
                              if (item := self.property(node, kind, prop, index)) is not None]
                names = [item["property"] for item in properties]
                if len(names) != len(set(names)):
                    self.diagnostic("XML_PROPERTY_DUPLICATE", "one record maps the same property more than once", node.path)
                for definition in self.types[kind].properties:
                    if definition.cardinality.required and definition.name not in names:
                        self.diagnostic("XML_REQUIRED_PROPERTY_MISSING", "required ontology property lacks source evidence: " + definition.name, node.path)
                item = {"source_identity": source_identity, "identity_parts": fields, "collection": collection,
                        "entity_type": kind, "source_path": node.path, "properties": properties,
                        "evidence": self.anchor(node), "identity_anchors": identity_anchors,
                        "type_anchors": type_anchors, "semantic_context": self.result["semantic_context"]}
                self.groups[collection][node.path] = item
                self.emit("entities", item)

    def relationships(self) -> None:
        node_by_path = {node.path: node for node in self.nodes}
        seen = set()
        for rule in _rows(self.mapping["relationships"], 64):
            _shape(rule, {"type", "kind", "from_collection", "to_collection"}, {"select", "target_select"})
            definition = self.relations.get(rule["type"])
            if definition is None or rule["from_collection"] not in self.groups or rule["to_collection"] not in self.groups:
                raise ValueError("relationship references an unknown collection or ontology type")
            if definition.properties and any(prop.cardinality.required for prop in definition.properties):
                raise ValueError("this XML contract cannot invent required relationship property values")
            source_group, target_group = self.groups[rule["from_collection"]], self.groups[rule["to_collection"]]
            pairs = []
            if rule["kind"] == "parent":
                if "select" in rule or "target_select" in rule:
                    raise ValueError("parent relationship cannot carry reference selectors")
                for target in target_group.values():
                    node = node_by_path[target["source_path"]]
                    source = self.nearest(node, rule["from_collection"])
                    if source:
                        pairs.append((source, target, None))
            elif rule["kind"] == "reference":
                if "select" not in rule or "target_select" not in rule:
                    raise ValueError("reference relationship requires source and target selectors")
                index: dict[str, list] = {}
                for target in target_group.values():
                    scalar = self.scalar(node_by_path[target["source_path"]], rule["target_select"])
                    if scalar:
                        self.mark(scalar, "binding", "reference target key")
                        index.setdefault(scalar.scalar.value, []).append(target)
                for source in source_group.values():
                    scalar = self.scalar(node_by_path[source["source_path"]], rule["select"])
                    if scalar is None or not scalar.scalar.value:
                        if scalar:
                            self.mark(scalar, "retained", "empty reference does not create a relationship")
                        continue
                    self.mark(scalar, "binding", "explicit source reference")
                    matches = index.get(scalar.scalar.value, [])
                    if len(matches) != 1:
                        code = "XML_REFERENCE_UNRESOLVED" if not matches else "XML_REFERENCE_AMBIGUOUS"
                        self.result["unresolved_references"].append({"type": rule["type"], "source_identity": source["source_identity"],
                            "reference": scalar.scalar.value, "evidence": self.anchor(scalar), "code": code})
                        self.diagnostic(code, "reference must identify exactly one selected source record", scalar.path)
                        continue
                    pairs.append((source, matches[0], scalar))
            else:
                raise ValueError("unsupported relationship construction kind")
            for source, target, reference in pairs:
                if len(self.result["relationships"]) >= MAX_RELATIONSHIPS:
                    raise ValueError("XML relationship budget exceeded")
                if not definition.allows_instances(source["entity_type"], target["entity_type"]):
                    self.diagnostic("XML_RELATIONSHIP_ENDPOINT_INVALID", "relationship endpoints violate ontology type pairs", source["source_path"])
                    continue
                identity = {"type": rule["type"], "source": source["source_identity"], "target": target["source_identity"]}
                checksum = _digest(identity)
                if checksum in seen:
                    self.diagnostic("XML_RELATIONSHIP_DUPLICATE", "duplicate relationship construction requires review", source["source_path"])
                    continue
                seen.add(checksum)
                self.emit("relationships", {**identity, "source_identity": "xml-rel:" + checksum,
                    "basis": rule["kind"], "evidence": self.anchor(reference) if reference else target["evidence"],
                    "structural_anchors": [source["evidence"], target["evidence"]],
                    "semantic_context": self.result["semantic_context"]})

    def run(self) -> dict:
        _shape(self.mapping, {"version", "mapping_id", "mapping_version", "root", "collections", "relationships",
                             "retained_fields", "semantic_context", "deferred_relationships"})
        if self.mapping["version"] != MAPPING_VERSION or len(_canonical(self.mapping)) > 65_536:
            raise ValueError("unsupported mapping version or size")
        for field in ("mapping_id", "mapping_version"):
            if not isinstance(self.mapping[field], str) or not 0 < len(self.mapping[field]) <= 128:
                raise ValueError("mapping identity must be a bounded string")
        _shape(self.mapping["root"], {"name"}, {"namespace_uri"})
        if self.root.name != self.mapping["root"]["name"] or self.root.namespace_uri != self.mapping["root"].get("namespace_uri"):
            raise ValueError("mapping root name or namespace does not match source")
        context = self.mapping["semantic_context"]
        if not isinstance(context, dict) or len(_canonical(context)) > 4_096 or context.get("runtime_event") is not False:
            raise ValueError("XML preflight context must explicitly exclude runtime events")
        if set(context) & {"status", "governance_status", "publication_status", "authority", "authority_level", "source_layer", "source_rank", "approved"}:
            raise ValueError("mapping context cannot assign source authority, approval or publication status")
        self.result["semantic_context"] = context
        self.retain(self.root, self.mapping["retained_fields"])
        self.records()
        self.relationships()
        for deferred in _rows(self.mapping["deferred_relationships"], 64):
            _shape(deferred, {"type", "collection", "reason"})
            if deferred["type"] not in self.relations or deferred["collection"] not in self.groups:
                raise ValueError("deferred relation must reference the active ontology and a collection")
            self.diagnostic("XML_SEMANTIC_BINDING_DEFERRED", str(deferred["reason"])[:512],
                            deferred["collection"], "warning")
            self.result["unresolved_references"].append({"type": deferred["type"], "collection": deferred["collection"],
                "record_count": len(self.groups[deferred["collection"]]), "reason": deferred["reason"],
                "code": "XML_SEMANTIC_BINDING_DEFERRED"})
        fields = []
        for path, field in sorted(self.inventory.items(), key=lambda row: row[1].scalar.start):
            disposition, reason = self.dispositions.get(path, ("unmapped", "no mapping or explicit retention rule"))
            fields.append({"path": path, "disposition": disposition, "reason": reason,
                           "char_start": field.scalar.start, "char_end": field.scalar.end})
        unknown = [field for field in fields if field["disposition"] == "unmapped"]
        if unknown:
            self.diagnostic("XML_FIELD_COVERAGE_INCOMPLETE", f"{len(unknown)} source fields require a mapping or explicit retention reason", unknown[0]["path"])
        self.result["coverage"] = {"source_elements": len(self.nodes), "source_fields": len(fields),
            "record_count": len(self.result["entities"]), "relationship_count": len(self.result["relationships"]),
            "unmapped_field_count": len(unknown), "fields": fields,
            "collections": {name: len(group) for name, group in self.groups.items()}}
        # A source-only workflow may retain a validly parsed source with semantic
        # gaps. Those gaps must not be confused with readiness for A-Box storage.
        self.result["valid"] = True
        self.result["complete"] = not unknown and not self.result["unresolved_references"] and not any(
            item["severity"] == "error" for item in self.result["diagnostics"])
        self.result["ontology_ready"] = False
        self.result["governance_disposition"] = "PREVIEW_ONLY"
        if len(_canonical(self.result)) > MAX_REPORT_CHARS:
            raise ValueError("XML preview report exceeds its output budget")
        return self.result


def preflight_xml_mapping(text: str, *, mapping: dict, tbox: TBoxVersion, source_checksum: str | None = None) -> dict:
    """Validate a caller-authorized mapping against unchanged normalized XML.

    ``source_checksum`` may be the retained original-byte hash from ParsedDocument;
    normalized_checksum independently binds every returned character range.
    Source identities are document-scoped preview keys, not adjudicated platform
    entity IDs. A successful preview does not authorize approval or publication.
    """
    preview = None
    try:
        if not isinstance(tbox, TBoxVersion):
            raise ValueError("preflight requires the selected ontology version")
        preview = _Preview(text, mapping, tbox, source_checksum)
        return preview.run()
    except (DocumentParseError, ValueError, TypeError, KeyError, OverflowError) as error:
        result = preview.result if preview else {
            "version": VERSION, "valid": False, "candidate_only": True, "publication_status": "not_published",
            "entities": [], "relationships": [], "unresolved_references": [], "coverage": {},
            "semantic_context": {}, "diagnostics": [],
        }
        result["valid"] = False
        result["complete"] = False
        result["ontology_ready"] = False
        result["governance_disposition"] = "PREVIEW_ONLY"
        # Contract failures never expose a partly executed plan as candidates.
        result["entities"] = []
        result["relationships"] = []
        result["unresolved_references"] = []
        result["coverage"] = {}
        result["diagnostics"].append({"severity": "error", "code": "XML_MAPPING_INVALID", "path": "$", "message": str(error)[:512]})
        return result
