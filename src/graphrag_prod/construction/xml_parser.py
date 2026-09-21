"""Bounded XML parsing with exact ranges in the retained source text.

This adapter describes syntax, never equipment semantics. XML stays XML in the
document/Chunk evidence chain; decoded values are separate from their original
spelling. No DTD, declared entity, schema retrieval or external resource is used.
"""

from __future__ import annotations

from array import array
from bisect import bisect_left
from dataclasses import dataclass, field
import re
from typing import Iterator
from xml.parsers import expat

from .parser import DocumentParseError, ParserLimits, _decode_utf8, _normalize_text

XML_PARSER_VERSION = "bounded-xml-spans:v1"
XML_MIME_TYPES = frozenset({"application/xml", "text/xml"})
_ATTRIBUTE = re.compile(r'''([^\s=/>]+)\s*=\s*(?:"([^"]*)"|'([^']*)')''')


@dataclass(frozen=True, slots=True)
class XmlScalar:
    """Decoded value and its exact original spelling (excluding delimiters)."""

    value: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class XmlElement:
    """A syntax node; paths are source locations, never business identities."""

    name: str
    path: str
    start: int
    start_tag_end: int
    end: int
    attributes: dict[str, XmlScalar]
    children: tuple[XmlElement, ...]
    text: tuple[XmlScalar, ...]
    namespace_uri: str | None = None

    def walk(self) -> Iterator[XmlElement]:
        """Yield source-ordered nodes without recursive traversal."""
        pending = [self]
        while pending:
            item = pending.pop()
            yield item
            pending.extend(reversed(item.children))


@dataclass(slots=True)
class _Frame:
    name: str
    path: str
    start: int
    start_tag_end: int
    self_closing: bool
    attributes: dict[str, XmlScalar]
    namespaces: dict[str, str]
    children: list[XmlElement] = field(default_factory=list)
    text: list[XmlScalar] = field(default_factory=list)
    child_counts: dict[str, int] = field(default_factory=dict)


def locate_xml(text: str, *, limits: ParserLimits | None = None) -> XmlElement:
    """Validate one XML document and locate its elements, attributes and text.

    Offsets are Unicode character offsets in ``text``, not serialized JSON or
    re-rendered XML. Call with ``ParsedDocument.normalized_text`` for evidence
    ranges compatible with the platform's immutable Chunk contract.
    """
    selected = limits or ParserLimits()
    if not isinstance(text, str):
        raise TypeError("XML source must be text")
    if not text.strip():
        raise DocumentParseError("XML source must not be empty")
    if len(text) > selected.max_normalized_chars:
        raise DocumentParseError("XML exceeds the configured character limit")
    try:
        payload = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise DocumentParseError("XML source must be valid UTF-8") from exc
    if len(payload) > selected.max_source_bytes:
        raise DocumentParseError("XML exceeds the configured byte limit")

    # Expat reports byte indexes. A compact, bounded index converts them without
    # repeatedly decoding prefixes (which would make large documents quadratic).
    offsets = array("I", [0])
    cursor = 0
    for character in text:
        cursor += len(character.encode("utf-8"))
        offsets.append(cursor)

    def char_offset(byte_offset: int) -> int:
        position = bisect_left(offsets, byte_offset)
        if position == len(offsets) or offsets[position] != byte_offset:
            raise DocumentParseError("XML parser returned an invalid source boundary")
        return position

    def tag_end(start: int) -> int:
        quote = None
        for position in range(start, len(text)):
            character = text[position]
            if quote:
                if character == quote:
                    quote = None
            elif character in {"'", '"'}:
                quote = character
            elif character == ">":
                return position + 1
        raise DocumentParseError("XML has an unterminated tag")

    parser = expat.ParserCreate(encoding="UTF-8", namespace_separator="\x1f")
    parser.namespace_prefixes = True
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    stack: list[_Frame] = []
    root: XmlElement | None = None
    nodes = 0
    text_fragments = 0
    total_attributes = 0
    total_path_chars = 0
    in_cdata = False
    namespace_declarations: dict[str, str] = {}

    def qualified_name(expanded: str) -> str:
        parts = expanded.split("\x1f")
        if len(parts) == 3:
            return f"{parts[2]}:{parts[1]}"
        return parts[-1]

    def namespace_start(prefix: str | None, uri: str | None) -> None:
        key = f"xmlns:{prefix}" if prefix else "xmlns"
        namespace_declarations[key] = uri or ""

    def forbidden(*_args: object) -> None:
        raise DocumentParseError("XML DTDs, declared entities and external references are not supported")

    def declaration(version: str, encoding: str | None, _standalone: int) -> None:
        if version != "1.0":
            raise DocumentParseError("only XML 1.0 is supported")
        if encoding and encoding.lower() not in {"utf-8", "utf8"}:
            raise DocumentParseError("XML encoding declaration must be UTF-8")

    def start_element(expanded_name: str, expanded_values: dict[str, str]) -> None:
        nonlocal nodes, total_attributes, total_path_chars
        name = qualified_name(expanded_name)
        values = {qualified_name(key): value for key, value in expanded_values.items()}
        values.update(namespace_declarations)
        namespace_declarations.clear()
        nodes += 1
        if nodes > selected.max_xml_nodes:
            raise DocumentParseError("XML exceeds the configured node limit")
        if len(stack) + 1 > selected.max_xml_depth:
            raise DocumentParseError("XML exceeds the configured nesting limit")
        if len(values) > selected.max_xml_attributes:
            raise DocumentParseError("XML exceeds the per-element attribute limit")
        total_attributes += len(values)
        if total_attributes > selected.max_xml_total_attributes:
            raise DocumentParseError("XML exceeds the total attribute limit")
        if len(name) > selected.max_xml_name_chars or any(
            len(key) > selected.max_xml_name_chars for key in values
        ):
            raise DocumentParseError("XML exceeds the configured name limit")
        start = char_offset(parser.CurrentByteIndex)
        end = tag_end(start)
        attributes: dict[str, XmlScalar] = {}
        for match in _ATTRIBUTE.finditer(text, start + 1 + len(name), end):
            key = match.group(1)
            group = 2 if match.group(2) is not None else 3
            left, right = match.span(group)
            if right - left > selected.max_xml_attribute_chars:
                raise DocumentParseError("XML exceeds the configured attribute value limit")
            if key not in values:
                raise DocumentParseError("XML attribute source positions could not be verified")
            attributes[key] = XmlScalar(values[key], left, right)
        if set(attributes) != set(values):
            raise DocumentParseError("XML attribute source positions could not be verified")
        namespaces = dict(stack[-1].namespaces) if stack else {"xml": "http://www.w3.org/XML/1998/namespace"}
        for key, value in values.items():
            if key == "xmlns":
                namespaces[""] = value
            elif key.startswith("xmlns:"):
                namespaces[key[6:]] = value
        if stack:
            parent = stack[-1]
            ordinal = parent.child_counts.get(name, 0) + 1
            parent.child_counts[name] = ordinal
            path = f"{parent.path}/{name}[{ordinal}]"
        else:
            path = f"/{name}[1]"
        total_path_chars += len(path)
        if len(path) > selected.max_xml_path_chars or total_path_chars > selected.max_xml_total_path_chars:
            raise DocumentParseError("XML exceeds the configured source path limit")
        stack.append(_Frame(
            name, path, start, end, text[start:end].rstrip().endswith("/>"),
            attributes, namespaces,
        ))

    def end_element(_name: str) -> None:
        nonlocal root
        frame = stack.pop()
        end = frame.start_tag_end if frame.self_closing else tag_end(char_offset(parser.CurrentByteIndex))
        prefix = frame.name.split(":", 1)[0] if ":" in frame.name else ""
        element = XmlElement(
            frame.name, frame.path, frame.start, frame.start_tag_end, end,
            frame.attributes, tuple(frame.children), tuple(frame.text),
            frame.namespaces.get(prefix) or None,
        )
        if stack:
            stack[-1].children.append(element)
        else:
            root = element

    def character_data(value: str) -> None:
        nonlocal text_fragments
        if not stack or not value:
            return
        text_fragments += 1
        if text_fragments > selected.max_xml_text_fragments:
            raise DocumentParseError("XML exceeds the configured text fragment limit")
        start = char_offset(parser.CurrentByteIndex)
        if not in_cdata and text[start:start + 1] == "&":
            end = text.find(";", start) + 1
            if end <= start:
                raise DocumentParseError("XML entity source position could not be verified")
        else:
            # XML normalizes CR/CRLF. The upload parser normalizes first, but
            # standalone callers may still use original line endings.
            end = start
            for character in value:
                if character == "\n" and text[end:end + 1] == "\r":
                    end += 2 if text[end:end + 2] == "\r\n" else 1
                else:
                    end += 1
        stack[-1].text.append(XmlScalar(value, start, end))

    def cdata_start() -> None:
        nonlocal in_cdata
        in_cdata = True

    def cdata_end() -> None:
        nonlocal in_cdata
        in_cdata = False

    parser.XmlDeclHandler = declaration
    parser.StartNamespaceDeclHandler = namespace_start
    parser.StartDoctypeDeclHandler = forbidden
    parser.EntityDeclHandler = forbidden
    parser.ExternalEntityRefHandler = forbidden
    parser.UnparsedEntityDeclHandler = forbidden
    parser.StartElementHandler = start_element
    parser.EndElementHandler = end_element
    parser.CharacterDataHandler = character_data
    parser.StartCdataSectionHandler = cdata_start
    parser.EndCdataSectionHandler = cdata_end
    try:
        parser.Parse(payload, True)
    except expat.ExpatError as exc:
        raise DocumentParseError(
            f"XML syntax error at line {exc.lineno}, column {exc.offset + 1}"
        ) from exc
    if root is None or stack:
        raise DocumentParseError("XML must contain exactly one complete root element")
    return root


@dataclass(frozen=True, slots=True)
class XmlDocumentParser:
    limits: ParserLimits
    mime_types: frozenset[str] = XML_MIME_TYPES

    def parse(self, payload: bytes) -> str:
        text = _normalize_text(_decode_utf8(payload))
        locate_xml(text, limits=self.limits)
        return text
