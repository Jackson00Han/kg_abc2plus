"""Source-authored industrial evaluation labels, independent of predictions.

Positive relevance and complete evidence alternatives describe different things:
the former is used unchanged by standard ranking metrics; the latter measures
whether a returned context can support the requested answer or its limitations.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable

from .contract import QUESTION_CLASSES
from .corpus import (
    ACCESS_GROUPS, FAMILY_TO_CONTRACT, IndustrialCorpus, build_corpus,
    compute_corpus_checksum,
)


GOLD_VERSION = "industrial-gold-v1.0.1"
CORPUS_CHECKSUM = "f79af571b20364e08d2a966da129714b417b6445cd46f5744339697050d06703"
DEFAULT_GOLD_PATH = Path(__file__).resolve().parents[3] / "datasets/industrial-v1/evaluation/gold.json"
DEFAULT_PDF_GOLD_PATH = DEFAULT_GOLD_PATH.with_name("pdf-integration.json")
SCHEMA_VERSION = "industrial-retrieval-gold-v1"
ANNOTATION_PROTOCOL = "industrial-source-relevance-v1"
_KEY = re.compile(r"^[a-z][a-z0-9-]{1,99}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ANSWERABILITY = frozenset({"SUPPORTED", "INSUFFICIENT", "AMBIGUOUS", "DENIED"})
_DEV_GROUPS = frozenset({"bkt-a01", "bkt-a02", "hvx-a01", "hvx-b01"})
_HOLDOUT_GROUPS = frozenset({"bkt-b01", "bkt-b02", "hvx-a02", "hvx-b02"})


class GoldValidationError(ValueError):
    """Gold identity, source evidence, permissions or annotation is invalid."""


def _text(value: object, maximum: int = 2_000) -> None:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise GoldValidationError("gold text must be trimmed, non-empty and bounded")


def _key(value: object) -> None:
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise GoldValidationError("gold reference must use a canonical key")


def _checksum(value: object) -> None:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise GoldValidationError("gold checksum must be a lowercase SHA-256 digest")


def _timestamp(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
    except (ValueError, TypeError) as error:
        raise GoldValidationError("gold cutoff must have an explicit timezone") from error
    return result


def _tuple(value: object, maximum: int = 100) -> None:
    if not isinstance(value, tuple) or len(value) > maximum:
        raise GoldValidationError("gold sequences must be immutable and bounded")


@dataclass(frozen=True, slots=True)
class EvidenceAnchor:
    document_key: str
    section_key: str
    char_start: int
    char_end: int
    document_checksum: str

    def __post_init__(self) -> None:
        _key(self.document_key)
        _key(self.section_key)
        _checksum(self.document_checksum)
        if type(self.char_start) is not int or type(self.char_end) is not int or not 0 <= self.char_start < self.char_end <= 50_000:
            raise GoldValidationError("gold evidence requires exact integer source offsets")

    @property
    def key(self) -> str:
        return f"{self.document_key}#{self.section_key}"


@dataclass(frozen=True, slots=True)
class GradedEvidence:
    anchor: EvidenceAnchor
    grade: int
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.anchor, EvidenceAnchor) or type(self.grade) is not int or not 1 <= self.grade <= 3:
            raise GoldValidationError("positive evidence requires an anchor and integer grade 1–3")
        _text(self.reason)


@dataclass(frozen=True, slots=True)
class GoldPrincipal:
    tenant_id: str
    access_groups: tuple[str, ...]

    def __post_init__(self) -> None:
        _key(self.tenant_id)
        _tuple(self.access_groups, 3)
        if not self.access_groups or any(not isinstance(g, str) for g in self.access_groups) or tuple(sorted(set(self.access_groups))) != self.access_groups or not set(self.access_groups) <= set(ACCESS_GROUPS):
            raise GoldValidationError("gold principal groups must be explicit and canonical")


@dataclass(frozen=True, slots=True)
class UserScope:
    family: str
    asset_keys: tuple[str, ...]
    include_family_references: bool
    published_at_lte: str | None
    origin: str

    def __post_init__(self) -> None:
        if not isinstance(self.family, str) or self.family not in FAMILY_TO_CONTRACT:
            raise GoldValidationError("gold scope requires a supported product family")
        _tuple(self.asset_keys, 8)
        for key in self.asset_keys:
            _key(key)
        if tuple(sorted(set(self.asset_keys))) != self.asset_keys or type(self.include_family_references) is not bool:
            raise GoldValidationError("gold scope keys and reference inclusion must be explicit")
        if self.published_at_lte is not None:
            _timestamp(self.published_at_lte)
        if self.origin not in {"USER_SUPPLIED", "FAMILY_ONLY_IDENTITY"}:
            raise GoldValidationError("gold must distinguish supplied scope from identity discovery")
        if self.origin == "FAMILY_ONLY_IDENTITY" and self.asset_keys:
            raise GoldValidationError("identity discovery cannot receive the expected asset as scope")


@dataclass(frozen=True, slots=True)
class GoldCase:
    case_id: str
    split: str
    event_group: str
    question_class: str
    question: str
    principal: GoldPrincipal
    user_scope: UserScope
    answerability: str
    evidence: tuple[GradedEvidence, ...]
    required_evidence_sets: tuple[tuple[str, ...], ...]
    forbidden_sections: tuple[EvidenceAnchor, ...]
    paired_case_id: str | None
    expected_answer_notes: str

    def __post_init__(self) -> None:
        _key(self.case_id)
        _key(self.event_group)
        _text(self.question)
        _text(self.expected_answer_notes)
        if self.split not in {"dev", "holdout"} or self.question_class not in QUESTION_CLASSES or self.answerability not in _ANSWERABILITY:
            raise GoldValidationError("unsupported gold class, split or answerability")
        if not isinstance(self.principal, GoldPrincipal) or not isinstance(self.user_scope, UserScope):
            raise GoldValidationError("gold runtime input must use explicit principal and scope")
        for values in (self.evidence, self.required_evidence_sets, self.forbidden_sections):
            _tuple(values)
        if any(not isinstance(item, GradedEvidence) for item in self.evidence) or any(not isinstance(item, EvidenceAnchor) for item in self.forbidden_sections):
            raise GoldValidationError("gold evidence records must be typed anchors")
        keys = tuple(item.anchor.key for item in self.evidence)
        if len(set(keys)) != len(keys) or keys != tuple(sorted(keys)):
            raise GoldValidationError("positive evidence must be unique and ordered")
        forbidden = tuple(item.key for item in self.forbidden_sections)
        if len(set(forbidden)) != len(forbidden) or forbidden != tuple(sorted(forbidden)) or set(keys) & set(forbidden):
            raise GoldValidationError("forbidden evidence must be unique and disjoint")
        for alternative in self.required_evidence_sets:
            _tuple(alternative, 20)
            if not alternative or any(not isinstance(key, str) for key in alternative) or tuple(sorted(set(alternative))) != alternative or not set(alternative) <= set(keys):
                raise GoldValidationError("complete evidence alternatives must reference positive anchors")
        alternatives = self.required_evidence_sets
        if len(set(alternatives)) != len(alternatives) or any(set(a) < set(b) for a in alternatives for b in alternatives):
            raise GoldValidationError("complete alternatives must be unique and minimal, not redundant supersets")
        if self.answerability == "DENIED":
            if self.evidence or alternatives or not self.forbidden_sections:
                raise GoldValidationError("denied record requests require forbidden sentinels and no positive target")
        elif not self.evidence or not alternatives:
            raise GoldValidationError("non-denied cases require positive evidence and complete alternatives")
        if self.paired_case_id is not None:
            _key(self.paired_case_id)

    @property
    def relevance(self) -> dict[str, int]:
        return {item.anchor.key: item.grade for item in self.evidence}

    @property
    def evidence_by_key(self) -> dict[str, EvidenceAnchor]:
        return {item.anchor.key: item.anchor for item in self.evidence}


@dataclass(frozen=True, slots=True)
class IndustrialGold:
    version: str
    corpus_checksum: str
    checksum: str
    annotation_protocol: str
    cases: tuple[GoldCase, ...]
    source_path: str

    def __post_init__(self) -> None:
        _text(self.version)
        _checksum(self.corpus_checksum)
        _checksum(self.checksum)
        _text(self.annotation_protocol)
        _text(self.source_path)
        _tuple(self.cases, 72)
        if any(not isinstance(case, GoldCase) for case in self.cases):
            raise GoldValidationError("gold requires immutable typed cases")


def _body(gold: IndustrialGold) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "version": gold.version,
            "corpus_checksum": gold.corpus_checksum, "annotation_protocol": gold.annotation_protocol,
            "cases": [asdict(case) for case in gold.cases]}


def compute_gold_checksum(gold: IndustrialGold) -> str:
    return hashlib.sha256(json.dumps(_body(gold), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def gold_to_dict(gold: IndustrialGold) -> dict[str, Any]:
    """Stable JSON payload; the caller's local path is not part of source identity."""
    return json.loads(json.dumps({**_body(gold), "checksum": gold.checksum}, ensure_ascii=False))


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GoldValidationError("duplicate JSON fields in gold")
        result[key] = value
    return result


def _read(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        payload = handle.read(2_000_001)
    if len(payload) > 2_000_000:
        raise GoldValidationError("gold exceeds the bounded source size")
    try:
        value = json.loads(payload, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GoldValidationError("gold must be UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise GoldValidationError("gold must be a JSON object")
    return value


def load_gold(path: str | Path | None = None, *, corpus: IndustrialCorpus | None = None) -> IndustrialGold:
    selected = Path(path) if path is not None else DEFAULT_GOLD_PATH
    raw = _read(selected)
    if set(raw) != {"schema_version", "version", "corpus_checksum", "annotation_protocol", "cases", "checksum"} or raw["schema_version"] != SCHEMA_VERSION:
        raise GoldValidationError("unsupported gold fields or schema")
    if not isinstance(raw["cases"], list) or len(raw["cases"]) != 72:
        raise GoldValidationError("version 1 gold requires all 72 reviewed cases")
    cases = []
    try:
        for value in raw["cases"]:
            if not isinstance(value, dict) or not isinstance(value.get("principal"), dict) or not isinstance(value.get("user_scope"), dict):
                raise GoldValidationError("gold case, principal and scope must be JSON objects")
            if not isinstance(value["principal"].get("access_groups"), list) or not isinstance(value["user_scope"].get("asset_keys"), list):
                raise GoldValidationError("gold scope and access groups must be explicit JSON arrays")
            if any(not isinstance(value.get(field), list) for field in ("evidence", "forbidden_sections", "required_evidence_sets")) or any(not isinstance(item, list) for item in value["required_evidence_sets"]):
                raise GoldValidationError("gold evidence and complete sets must be JSON arrays")
            value = dict(value)
            value["principal"] = GoldPrincipal(**{**value["principal"], "access_groups": tuple(value["principal"]["access_groups"])})
            value["user_scope"] = UserScope(**{**value["user_scope"], "asset_keys": tuple(value["user_scope"]["asset_keys"])})
            if any(not isinstance(item, dict) or set(item) != {"anchor", "grade", "reason"} for item in value["evidence"]):
                raise GoldValidationError("graded evidence has missing or unexpected fields")
            value["evidence"] = tuple(GradedEvidence(anchor=EvidenceAnchor(**item["anchor"]), grade=item["grade"], reason=item["reason"]) for item in value["evidence"])
            value["forbidden_sections"] = tuple(EvidenceAnchor(**item) for item in value["forbidden_sections"])
            value["required_evidence_sets"] = tuple(tuple(item) for item in value["required_evidence_sets"])
            cases.append(GoldCase(**value))
        gold = IndustrialGold(raw["version"], raw["corpus_checksum"], raw["checksum"], raw["annotation_protocol"], tuple(cases), str(selected.resolve()))
    except (KeyError, TypeError, AttributeError) as error:
        raise GoldValidationError("gold records have invalid fields or types") from error
    validate_gold(gold, corpus=corpus)
    return gold


def _within_scope(document: Any, scope: UserScope) -> bool:
    if document.family != scope.family:
        return False
    if scope.asset_keys and not set(scope.asset_keys) & set(document.asset_keys):
        if not scope.include_family_references or document.asset_keys or document.source_kind != "CURATED_REFERENCE":
            return False
    return scope.published_at_lte is None or _timestamp(document.published_at) <= _timestamp(scope.published_at_lte)


def validate_gold(gold: IndustrialGold, *, corpus: IndustrialCorpus | None = None) -> None:
    selected = corpus or build_corpus()
    if gold.version != GOLD_VERSION or gold.annotation_protocol != ANNOTATION_PROTOCOL or gold.corpus_checksum != CORPUS_CHECKSUM or selected.manifest_checksum != CORPUS_CHECKSUM or compute_corpus_checksum(selected) != CORPUS_CHECKSUM:
        raise GoldValidationError("gold must match the reviewed corpus and annotation versions")
    _checksum(gold.checksum)
    if gold.checksum != compute_gold_checksum(gold):
        raise GoldValidationError("gold semantic checksum does not match its annotations")
    _tuple(gold.cases, 72)
    if any(not isinstance(case, GoldCase) for case in gold.cases):
        raise GoldValidationError("gold cases must be immutable typed records")
    if len(gold.cases) != 72 or tuple(case.case_id for case in gold.cases) != tuple(f"industrial-gold-{n:03d}" for n in range(1, 73)):
        raise GoldValidationError("gold requires all cases in canonical order")
    if Counter(case.split for case in gold.cases) != {"dev": 36, "holdout": 36}:
        raise GoldValidationError("gold requires the reviewed event-group split")
    expected_classes = {key: 16 if key == "unauthorized" else 8 for key in QUESTION_CLASSES}
    if Counter(case.question_class for case in gold.cases) != expected_classes:
        raise GoldValidationError("all eight question classes and paired controls are required")
    documents = {document.key: document for document in selected.documents}
    assets = {entity.key: entity for entity in selected.entities if entity.type_name == "InstalledAsset"}
    case_by_id = {case.case_id: case for case in gold.cases}
    for case in gold.cases:
        expected_split = "dev" if case.event_group in _DEV_GROUPS else "holdout" if case.event_group in _HOLDOUT_GROUPS else None
        if case.split != expected_split:
            raise GoldValidationError("event groups cannot cross dev and holdout")
        if case.case_id != "industrial-gold-064" and case.principal.tenant_id != selected.tenant_id:
            raise GoldValidationError("only the declared foreign control uses another tenant")
        if case.question_class == "identity":
            if case.user_scope.origin != "FAMILY_ONLY_IDENTITY" or case.user_scope.asset_keys:
                raise GoldValidationError("identity cases must not receive an oracle asset filter")
        elif case.user_scope.origin != "USER_SUPPLIED":
            raise GoldValidationError("non-identity filters must be declared as user-supplied")
        for key in case.user_scope.asset_keys:
            asset = assets.get(key)
            if asset is None or documents[asset.document_key].family != case.user_scope.family:
                raise GoldValidationError("user asset scope must resolve within its product family")
        for anchor, positive in [(item.anchor, True) for item in case.evidence] + [(item, False) for item in case.forbidden_sections]:
            document = documents.get(anchor.document_key)
            if document is None:
                raise GoldValidationError("gold evidence references an unknown document")
            try:
                section = document.section(anchor.section_key)
            except ValueError as error:
                raise GoldValidationError("gold evidence references an unknown section") from error
            if (anchor.char_start, anchor.char_end) != (section.char_start, section.char_end) or anchor.document_checksum != hashlib.sha256(document.text.encode("utf-8")).hexdigest():
                raise GoldValidationError("gold evidence differs from its exact reviewed source range")
            authorized = case.principal.tenant_id == selected.tenant_id and bool(set(case.principal.access_groups) & set(document.access_groups))
            if positive and (not authorized or not _within_scope(document, case.user_scope)):
                raise GoldValidationError("positive evidence is outside declared ACL or user scope")
            if not positive and authorized:
                raise GoldValidationError("forbidden ACL sentinel cannot be an authorized source")
            if not positive and not _within_scope(document, case.user_scope):
                raise GoldValidationError("forbidden request sentinel must match the requested source scope")
        if case.question_class == "unauthorized":
            other = case_by_id.get(case.paired_case_id)
            if other is None or other.paired_case_id != case.case_id or other.split != case.split or other.event_group != case.event_group or other.question != case.question or other.user_scope != case.user_scope:
                raise GoldValidationError("authorization controls must retain identical question, scope and split")
            if {case.answerability, other.answerability} != {"SUPPORTED", "DENIED"}:
                raise GoldValidationError("authorization pairs require denied and supported populations")
            denied, allowed = (case, other) if case.answerability == "DENIED" else (other, case)
            if not {item.key for item in denied.forbidden_sections} <= set(allowed.relevance):
                raise GoldValidationError("authorized control must be able to retrieve protected sentinel evidence")
        elif case.paired_case_id is not None:
            raise GoldValidationError("only authorization cases carry a paired population")
    foreign = case_by_id["industrial-gold-064"]
    if foreign.principal != GoldPrincipal("tenant-alpha", ("public",)):
        raise GoldValidationError("foreign-tenant control requires the active legacy public population")
    if case_by_id["industrial-gold-033"].user_scope.published_at_lte != "2026-04-11T18:00:00+08:00":
        raise GoldValidationError("as-of cutoff must match the initial report publication time")


def complete_evidence_covered(case: GoldCase, returned_anchor_keys: Iterable[str]) -> bool:
    """Alternative complete-set coverage; never a replacement for ranking recall."""
    available = set(returned_anchor_keys)
    return bool(case.required_evidence_sets) and any(set(alternative) <= available for alternative in case.required_evidence_sets)


def recall_ceiling(positive_count: int, k: int = 5) -> float | None:
    if type(positive_count) is not int or positive_count < 0 or type(k) is not int or k < 1:
        raise GoldValidationError("recall ceiling requires non-negative count and positive integer K")
    return min(k, positive_count) / positive_count if positive_count else None


def gold_report(gold: IndustrialGold) -> dict[str, Any]:
    ceilings = [{"case_id": case.case_id, "positive_sections": len(case.evidence),
                 "recall_at_5_ceiling": recall_ceiling(len(case.evidence))} for case in gold.cases]
    by_split = {}
    for split in ("dev", "holdout", "all"):
        selected = [case for case in gold.cases if split == "all" or case.split == split]
        values = [recall_ceiling(len(case.evidence)) for case in selected if case.evidence]
        by_split[split] = {"cases": len(selected), "positive_target_cases": len(values),
                           "mean_recall_at_5_ceiling": sum(values) / len(values)}
    return {"schema_version": "industrial-gold-report-v1", "gold_version": gold.version,
            "gold_checksum": gold.checksum, "corpus_checksum": gold.corpus_checksum,
            "annotation_protocol": gold.annotation_protocol,
            "question_classes": dict(sorted(Counter(case.question_class for case in gold.cases).items())),
            "answerability": dict(sorted(Counter(case.answerability for case in gold.cases).items())),
            "splits": by_split, "case_recall_ceilings": ceilings,
            "ceiling_is_a_bound_not_a_prediction": True,
            "holdout_scope": "event-group; shared references and cross-asset identity passages are allowed",
            "technical_sme_review": "PENDING", "production_candidate_eligible": False}


def load_pdf_gold(path: str | Path | None = None) -> dict[str, Any]:
    """Validate separate PDF annotations without requiring or opening the cache.

    Integration execution must additionally validate the original and normalized
    artifacts and resolve every pinned range; this offline check is not a passed
    PDF retrieval run.
    """
    from .sources import load_source_catalog

    raw = _read(Path(path) if path is not None else DEFAULT_PDF_GOLD_PATH)
    if set(raw) != {"schema_version", "version", "requires_external_cache", "included_in_primary_gold_metrics", "cases", "checksum"}:
        raise GoldValidationError("PDF gold fields differ from the integration schema")
    if raw["schema_version"] != "industrial-pdf-integration-gold-v1" or raw["version"] != "industrial-pdf-gold-v1.0.0" or raw["requires_external_cache"] is not True or raw["included_in_primary_gold_metrics"] is not False:
        raise GoldValidationError("PDF integration must remain separate from authored offline metrics")
    _checksum(raw["checksum"])
    digest_body = {key: value for key, value in raw.items() if key != "checksum"}
    digest = hashlib.sha256(json.dumps(digest_body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    if digest != raw["checksum"]:
        raise GoldValidationError("PDF gold checksum differs from its source annotations")
    if not isinstance(raw["cases"], list) or len(raw["cases"]) != 3:
        raise GoldValidationError("PDF gold retains one cross-page and two diagnostic table cases")
    catalog = load_source_catalog(DEFAULT_GOLD_PATH.parents[1] / "sources.json")
    sources = {source.source_id: source for source in catalog.sources}
    for number, case in enumerate(raw["cases"], 1):
        expected = {"case_id", "question", "source_id", "embedded_revision", "physical_pages",
                    "original_checksum", "artifact_checksum", "normalized_checksum", "parser_version",
                    "splitter_signature", "suggested_artifact_filename", "required_chunks",
                    "expected_evidence_notes", "answerability", "family", "source_kind", "required_evidence_sets"}
        if not isinstance(case, dict) or set(case) != expected or case["case_id"] != f"industrial-pdf-{number:03d}":
            raise GoldValidationError("PDF integration cases require canonical identity and fields")
        for field in ("question", "expected_evidence_notes", "parser_version", "splitter_signature"):
            _text(case[field])
        for field in ("original_checksum", "artifact_checksum", "normalized_checksum"):
            _checksum(case[field])
        _key(case["source_id"])
        source = sources.get(case["source_id"])
        if source is None or source.applicability != "CORE" or case["original_checksum"] != source.sha256 or case["embedded_revision"] != source.embedded_revision:
            raise GoldValidationError("PDF integration source differs from the reviewed official edition")
        expected_family = {"canalis-kt-installation": "canalis-kt", "evopact-hvx-up-to-24kv": "evopact-hvx-up24"}.get(case["source_id"])
        if expected_family is None or case["family"] != expected_family or case["source_kind"] != "OFFICIAL_PUBLICATION":
            raise GoldValidationError("PDF integration requires exact family and official-publication scope")
        filename = case["suggested_artifact_filename"]
        if not isinstance(filename, str) or not re.fullmatch(r"[a-z][a-z0-9.-]{1,100}\.json", filename):
            raise GoldValidationError("PDF artifact hint must be a basename, never a local cache path")
        pages = case["physical_pages"]
        if not isinstance(pages, list) or not pages or any(type(page) is not int or not 1 <= page <= source.physical_pages for page in pages) or pages != sorted(set(pages)):
            raise GoldValidationError("PDF physical pages must be exact ordered source pages")
        chunks = case["required_chunks"]
        if not isinstance(chunks, list) or not 2 <= len(chunks) <= 8 or case["answerability"] != "SUPPORTED":
            raise GoldValidationError("PDF integration requires multiple supporting fragments")
        ordinals = []
        cursor = -1
        for chunk in chunks:
            if not isinstance(chunk, dict) or set(chunk) != {"ordinal", "page_number", "char_start", "char_end", "text_checksum"}:
                raise GoldValidationError("PDF gold requires exact chunk pin fields")
            if any(type(chunk[field]) is not int for field in ("ordinal", "page_number", "char_start", "char_end")):
                raise GoldValidationError("PDF gold locations must use integer scalars")
            if chunk["ordinal"] < 0 or chunk["page_number"] not in pages or not 0 <= chunk["char_start"] < chunk["char_end"] <= 500_000 or chunk["char_start"] < cursor:
                raise GoldValidationError("PDF gold ranges or pages are invalid")
            _checksum(chunk["text_checksum"])
            cursor = chunk["char_end"]
            ordinals.append(chunk["ordinal"])
        if ordinals != sorted(set(ordinals)):
            raise GoldValidationError("PDF required ordinals must be unique and ordered")
        alternatives = case["required_evidence_sets"]
        if not isinstance(alternatives, list) or not alternatives or any(not isinstance(item, list) or not item or any(type(ordinal) is not int for ordinal in item) or item != sorted(set(item)) or not set(item) <= set(ordinals) for item in alternatives):
            raise GoldValidationError("PDF complete sets must reference the pinned fragment ordinals")
        if len({tuple(item) for item in alternatives}) != len(alternatives) or any(set(a) < set(b) for a in alternatives for b in alternatives):
            raise GoldValidationError("PDF complete alternatives must be unique and minimal")
    return raw
