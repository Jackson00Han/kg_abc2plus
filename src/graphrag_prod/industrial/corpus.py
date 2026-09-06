"""Deterministic authored industrial corpus, with exact semantic-section evidence.

The checked-in Markdown is the source, not output of an online generator. The
builder neither downloads manuals nor predicts diagnoses or retrieval labels.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from typing import Any


CORPUS_VERSION = "industrial-corpus-v1.0.0"
TENANT_ID = "industrial-schneider-demo"
ACCESS_GROUPS = ("public", "engineering", "maintenance")
FAMILY_TO_CONTRACT = {
    "canalis-kt": "CANALIS_KT",
    "evopact-hvx-up24": "EVOPACT_HVX_UP_TO_24KV",
}
SECTION_SPLITTER_VERSION = "industrial-authored-sections:v1"
DEFAULT_CORPUS_ROOT = Path(__file__).resolve().parents[3] / "datasets" / "industrial-v1" / "corpus"
_KEY = re.compile(r"^[a-z][a-z0-9-]{1,99}$")
_SECTION = re.compile(r"^## ([a-z][a-z0-9-]+) \| (.+)$", re.MULTILINE)
_SOURCE_KINDS = frozenset({"CURATED_REFERENCE", "SYNTHETIC_FIELD_RECORD"})


class CorpusValidationError(ValueError):
    """The authored corpus violates its identity, provenance, or scale contract."""


def _key(value: str) -> None:
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise CorpusValidationError("corpus keys must be bounded stable identifiers")


def _text(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise CorpusValidationError("corpus metadata must be non-empty trimmed text")


def _digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CorpusValidationError("duplicate JSON field in corpus index")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class SourceReference:
    source_id: str
    physical_pages: tuple[int, ...]
    embedded_revision: str

    def __post_init__(self) -> None:
        _key(self.source_id)
        _text(self.embedded_revision)
        if (
            not isinstance(self.physical_pages, tuple)
            or not self.physical_pages
            or any(type(page) is not int or page < 1 for page in self.physical_pages)
            or tuple(sorted(set(self.physical_pages))) != self.physical_pages
        ):
            raise CorpusValidationError("source reference requires ordered physical pages")


@dataclass(frozen=True, slots=True)
class CorpusSection:
    key: str
    title: str
    char_start: int
    char_end: int

    def __post_init__(self) -> None:
        _key(self.key)
        _text(self.title)
        if (
            type(self.char_start) is not int or type(self.char_end) is not int
            or self.char_start < 0 or self.char_end <= self.char_start
            or self.char_end - self.char_start > 1_200
        ):
            raise CorpusValidationError("section must be an exact bounded non-empty range")


@dataclass(frozen=True, slots=True)
class CorpusDocument:
    key: str
    title: str
    source_kind: str
    family: str
    access_groups: tuple[str, ...]
    published_at: str
    text: str
    sections: tuple[CorpusSection, ...]
    source_refs: tuple[SourceReference, ...]
    asset_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _key(self.key)
        _text(self.title)
        if self.source_kind not in _SOURCE_KINDS or self.family not in FAMILY_TO_CONTRACT:
            raise CorpusValidationError("unsupported corpus source kind or family")
        if (
            not isinstance(self.access_groups, tuple) or not self.access_groups
            or tuple(sorted(set(self.access_groups))) != self.access_groups
            or not set(self.access_groups).issubset(ACCESS_GROUPS)
        ):
            raise CorpusValidationError("corpus access groups must be explicit and canonical")
        if (
            not isinstance(self.asset_keys, tuple) or len(self.asset_keys) > 8
            or any(not isinstance(key, str) for key in self.asset_keys)
            or tuple(sorted(set(self.asset_keys))) != self.asset_keys
        ):
            raise CorpusValidationError("primary asset keys must be immutable, bounded, and canonical")
        for key in self.asset_keys:
            _key(key)
        try:
            timestamp = datetime.fromisoformat(self.published_at)
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError
        except (ValueError, TypeError) as error:
            raise CorpusValidationError("publication time requires an explicit timezone") from error
        if (
            not isinstance(self.text, str) or not self.text.strip() or len(self.text) > 50_000
            or unicodedata.normalize("NFC", self.text) != self.text or "\r" in self.text
            or any(ord(c) < 32 and c not in "\n\t" for c in self.text)
        ):
            raise CorpusValidationError("authored source must be bounded NFC text with LF newlines")
        if not isinstance(self.sections, tuple) or not self.sections:
            raise CorpusValidationError("document requires immutable semantic sections")
        cursor = 0
        keys: set[str] = set()
        for section in self.sections:
            if not isinstance(section, CorpusSection) or section.char_start != cursor:
                raise CorpusValidationError("document sections must be gapless")
            if section.key in keys:
                raise CorpusValidationError("document section keys must be unique")
            keys.add(section.key)
            cursor = section.char_end
        if cursor != len(self.text):
            raise CorpusValidationError("document sections must cover all source text")
        if not isinstance(self.source_refs, tuple) or any(
            not isinstance(item, SourceReference) for item in self.source_refs
        ):
            raise CorpusValidationError("source references must be immutable records")
        if self.source_kind == "CURATED_REFERENCE":
            if not self.source_refs or "SME_REVIEW_PENDING" not in self.text:
                raise CorpusValidationError("curated references require bibliography and review status")
        elif "SYNTHETIC_FIELD_RECORD" not in self.text or "合成" not in self.text:
            raise CorpusValidationError("field records must disclose their synthetic origin")

    def section(self, key: str) -> CorpusSection:
        for section in self.sections:
            if section.key == key:
                return section
        raise CorpusValidationError("unknown document section")

    def section_text(self, key: str) -> str:
        section = self.section(key)
        return self.text[section.char_start:section.char_end]


@dataclass(frozen=True, slots=True)
class CorpusEntity:
    key: str
    type_name: str
    name: str
    identity: str
    document_key: str
    section_key: str
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for key in (self.key, self.document_key, self.section_key):
            _key(key)
        _text(self.type_name)
        _text(self.name)
        if self.identity != f"industrial:{self.key}":
            raise CorpusValidationError("entity identity must use its stable industrial key")
        if not isinstance(self.aliases, tuple) or len(set(self.aliases)) != len(self.aliases):
            raise CorpusValidationError("aliases must be immutable and unique per entity")
        for alias in self.aliases:
            _text(alias)


@dataclass(frozen=True, slots=True)
class CorpusRelationship:
    key: str
    subject_key: str
    predicate: str
    object_key: str
    document_key: str
    section_key: str

    def __post_init__(self) -> None:
        for key in (self.key, self.subject_key, self.object_key, self.document_key, self.section_key):
            _key(key)
        if not re.fullmatch(r"[A-Z][A-Z_]+", self.predicate):
            raise CorpusValidationError("relationship predicate must be canonical")


@dataclass(frozen=True, slots=True)
class IndustrialCorpus:
    version: str
    tenant_id: str
    documents: tuple[CorpusDocument, ...]
    entities: tuple[CorpusEntity, ...]
    relationships: tuple[CorpusRelationship, ...]
    manifest_checksum: str

    def document(self, key: str) -> CorpusDocument:
        for document in self.documents:
            if document.key == key:
                return document
        raise CorpusValidationError("unknown corpus document")

    @property
    def chunk_count(self) -> int:
        return sum(len(document.sections) for document in self.documents)


def _sections(text: str) -> tuple[CorpusSection, ...]:
    headings = list(_SECTION.finditer(text))
    if not headings:
        raise CorpusValidationError("authored document has no named semantic sections")
    return tuple(
        CorpusSection(
            key=match[1], title=match[2], char_start=match.start() if number else 0,
            char_end=headings[number + 1].start() if number + 1 < len(headings) else len(text),
        )
        for number, match in enumerate(headings)
    )


def build_corpus(root: Path | None = None) -> IndustrialCorpus:
    """Read authored files, validate evidence, and rebuild the same immutable view."""
    selected = (root or DEFAULT_CORPUS_ROOT).resolve()
    with (selected / "index.json").open("rb") as handle:
        payload = handle.read(1_000_001)
    if len(payload) > 1_000_000:
        raise CorpusValidationError("corpus index exceeds its byte limit")
    raw = json.loads(payload, object_pairs_hook=_unique_object)
    if set(raw) != {"schema_version", "version", "tenant_id", "family_to_contract", "documents", "entities", "relationships"}:
        raise CorpusValidationError("corpus index has missing or unexpected fields")
    if (
        raw["schema_version"] != "industrial-authored-corpus-v1"
        or raw["version"] != CORPUS_VERSION or raw["tenant_id"] != TENANT_ID
        or raw["family_to_contract"] != FAMILY_TO_CONTRACT
    ):
        raise CorpusValidationError("corpus identity or family mapping differs from its contract")
    if not isinstance(raw["documents"], list) or not 32 <= len(raw["documents"]) <= 80:
        raise CorpusValidationError("corpus index requires a bounded document array")
    if any(not isinstance(raw[name], list) or len(raw[name]) > 150 for name in ("entities", "relationships")):
        raise CorpusValidationError("corpus graph seeds exceed their array bounds")
    documents = []
    for record in raw["documents"]:
        record = dict(record)
        filename = record.pop("file")
        if not isinstance(filename, str) or not re.fullmatch(r"[a-z][a-z0-9-]+\.md", filename):
            raise CorpusValidationError("source filename must be a bounded Markdown basename")
        path = selected / "documents" / filename
        if path.is_symlink():
            raise CorpusValidationError("authored sources cannot be symbolic links")
        with path.open("rb") as handle:
            encoded = handle.read(200_001)
        if len(encoded) > 200_000:
            raise CorpusValidationError("authored source exceeds its byte limit")
        text = encoded.decode("utf-8")
        record["access_groups"] = tuple(record["access_groups"])
        record["asset_keys"] = tuple(record.get("asset_keys", ()))
        record["source_refs"] = tuple(
            SourceReference(**{**item, "physical_pages": tuple(item["physical_pages"])})
            for item in record["source_refs"]
        )
        documents.append(CorpusDocument(**record, text=text, sections=_sections(text)))
    entities = tuple(CorpusEntity(**{**item, "aliases": tuple(item.get("aliases", ()))}) for item in raw["entities"])
    relationships = tuple(CorpusRelationship(**item) for item in raw["relationships"])
    corpus = IndustrialCorpus(CORPUS_VERSION, TENANT_ID, tuple(documents), entities, relationships, "")
    validate_corpus(corpus)
    checksum = compute_corpus_checksum(corpus)
    return IndustrialCorpus(corpus.version, corpus.tenant_id, corpus.documents, entities, relationships, checksum)


def compute_corpus_checksum(corpus: IndustrialCorpus) -> str:
    """Compute identity from all source bytes and evidence seeds, never a cached pin."""
    return _digest({
        "version": corpus.version, "tenant_id": corpus.tenant_id,
        "family_to_contract": FAMILY_TO_CONTRACT,
        "documents": [asdict(item) for item in corpus.documents],
        "entities": [asdict(item) for item in corpus.entities],
        "relationships": [asdict(item) for item in corpus.relationships],
    })


def validate_corpus(corpus: IndustrialCorpus) -> None:
    """Validate diversity and evidence without manufacturing answer predictions."""
    documents = {item.key: item for item in corpus.documents}
    entities = {item.key: item for item in corpus.entities}
    if len(documents) != len(corpus.documents) or len(entities) != len(corpus.entities):
        raise CorpusValidationError("document and entity keys must be unique")
    if len({item.key for item in corpus.relationships}) != len(corpus.relationships):
        raise CorpusValidationError("relationship keys must be unique")
    if not 32 <= len(documents) <= 80 or not 300 <= corpus.chunk_count <= 1_000:
        raise CorpusValidationError("corpus must contain 32–80 documents and 300–1000 meaningful sections")
    if {item.family for item in corpus.documents} != set(FAMILY_TO_CONTRACT):
        raise CorpusValidationError("both industrial families must be represented")
    if set().union(*(set(item.access_groups) for item in corpus.documents)) != set(ACCESS_GROUPS):
        raise CorpusValidationError("all three industrial access groups must be represented")
    field_documents = [item for item in corpus.documents if item.source_kind == "SYNTHETIC_FIELD_RECORD"]
    field_sections = [item.section_text(section.key) for item in field_documents for section in item.sections]
    if len(field_documents) < 32 or any(not 8 <= len(item.sections) <= 12 for item in field_documents):
        raise CorpusValidationError("field corpus requires multiple substantial sections per document")
    if min(map(len, field_sections), default=0) < 90 or sum(map(len, field_sections)) < len(field_sections) * 145:
        raise CorpusValidationError("field sections are too short to supply meaningful independent context")
    if len(set(field_sections)) != len(field_sections):
        raise CorpusValidationError("repeated field sections cannot inflate corpus scale")
    if len({item.text for item in corpus.documents}) != len(corpus.documents):
        raise CorpusValidationError("duplicate documents cannot inflate corpus scale")
    for document in corpus.documents:
        if document.source_kind == "SYNTHETIC_FIELD_RECORD" and not document.asset_keys:
            raise CorpusValidationError("field documents require explicit primary asset scope")
        if document.source_kind == "CURATED_REFERENCE" and document.asset_keys:
            raise CorpusValidationError("product references cannot claim an installed-asset scope")
        for key in document.asset_keys:
            asset = entities.get(key)
            if asset is None or asset.type_name != "InstalledAsset":
                raise CorpusValidationError("primary asset scope must reference an installed asset")
            if asset.name not in document.text:
                raise CorpusValidationError("primary asset requires exact document evidence")
            asset_document = documents.get(asset.document_key)
            if asset_document is None or asset_document.family != document.family:
                raise CorpusValidationError("primary asset scope cannot cross product families")
    for entity in corpus.entities:
        document = documents.get(entity.document_key)
        if document is None or entity.name not in document.section_text(entity.section_key):
            raise CorpusValidationError(f"entity {entity.key} lacks exact section evidence")
    for relation in corpus.relationships:
        document = documents.get(relation.document_key)
        subject, target = entities.get(relation.subject_key), entities.get(relation.object_key)
        if document is None or subject is None or target is None:
            raise CorpusValidationError("relationship references an unknown document or entity")
        evidence = document.section_text(relation.section_key)
        if subject.name not in evidence or target.name not in evidence:
            raise CorpusValidationError(f"relationship {relation.key} lacks both endpoint names in its evidence")
    # Conservative accounting: one entity mention per seed and two additional
    # endpoint mentions per relationship, plus each entity/relationship record.
    if 2 * len(entities) + 3 * len(corpus.relationships) > 500:
        raise CorpusValidationError("selected graph exceeds the 500-record publication budget")
    if sum(item.type_name == "InstalledAsset" for item in entities.values()) < 8:
        raise CorpusValidationError("at least eight installed assets are required")


def corpus_report(corpus: IndustrialCorpus) -> dict[str, Any]:
    """Deterministic committed manifest: source hashes and exact section ranges."""
    return {
        "schema_version": "industrial-corpus-manifest-v1",
        "version": corpus.version, "tenant_id": corpus.tenant_id,
        "family_to_contract": FAMILY_TO_CONTRACT,
        "manifest_checksum": corpus.manifest_checksum,
        "counts": {
            "documents": len(corpus.documents), "chunks": corpus.chunk_count,
            "entities": len(corpus.entities), "relationships": len(corpus.relationships),
            "record_upper_bound": 2 * len(corpus.entities) + 3 * len(corpus.relationships),
            "source_kinds": dict(sorted(Counter(item.source_kind for item in corpus.documents).items())),
            "families": dict(sorted(Counter(item.family for item in corpus.documents).items())),
        },
        "documents": [
            {"key": item.key, "source_kind": item.source_kind, "family": item.family,
             "access_groups": list(item.access_groups), "published_at": item.published_at,
             "asset_keys": list(item.asset_keys),
             "sha256": hashlib.sha256(item.text.encode("utf-8")).hexdigest(),
             "characters": len(item.text), "sections": [asdict(section) for section in item.sections]}
            for item in corpus.documents
        ],
    }
