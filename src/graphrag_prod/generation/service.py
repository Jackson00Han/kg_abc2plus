"""Fail-closed orchestration for structured, source-grounded answers.

The exact-excerpt and lexical-containment checks below are deliberately
conservative runtime safety gates. They reject unsupported sourced wording but
do not claim to implement complete semantic entailment; adjudicated evaluation
must measure that separately.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import re
import unicodedata
from typing import Any

from .models import (
    AnswerCitation,
    AnswerModel,
    AnswerModelRequest,
    AnswerResult,
    AnswerStatus,
    Claim,
    Conflict,
    GenerationLimits,
    GenerationRequest,
)
from .prompt import LabelledContext, build_prompt, label_context


INVALID_MODEL_OUTPUT = "invalid_model_output"
INVALID_CONTEXT = "invalid_context"
GENERATION_LIMIT_EXCEEDED = "generation_limit_exceeded"
_EXPECTED_TOP_LEVEL = frozenset({"status", "claims", "conflicts"})
_EXPECTED_CLAIM = frozenset(
    {"text", "material", "inference", "citation_ids", "evidence"}
)
_EXPECTED_EVIDENCE = frozenset({"citation_id", "quote"})
_EXPECTED_CONFLICT = frozenset({"topic", "alternatives"})
_EXPECTED_ALTERNATIVE = frozenset({"text", "citation_ids", "evidence"})
_INLINE_LABEL = re.compile(
    r"\[\s*(?:S\s*\d+|source\b[^\]]*|citation\b[^\]]*)\s*\]",
    re.IGNORECASE,
)
_TOKEN = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "with",
    }
)
_RELATION_TERMS = frozenset({"and", "by", "from", "or", "to"})
_MATERIAL_PREPOSITION_TERMS = frozenset(
    {
        "about",
        "across",
        "against",
        "along",
        "among",
        "as",
        "at",
        "beside",
        "besides",
        "beyond",
        "concerning",
        "despite",
        "down",
        "during",
        "following",
        "for",
        "in",
        "inside",
        "into",
        "like",
        "near",
        "of",
        "off",
        "on",
        "onto",
        "opposite",
        "outside",
        "past",
        "per",
        "regarding",
        "round",
        "since",
        "than",
        "through",
        "throughout",
        "toward",
        "towards",
        "underneath",
        "unlike",
        "upon",
        "via",
        "with",
        "within",
    }
)
_NEGATION_TERMS = frozenset(
    {
        "ain't",
        "cannot",
        "can't",
        "couldn't",
        "didn't",
        "denied",
        "denies",
        "deny",
        "denying",
        "doesn't",
        "don't",
        "false",
        "falsely",
        "hadn't",
        "hardly",
        "hasn't",
        "haven't",
        "isn't",
        "mustn't",
        "neither",
        "never",
        "no",
        "nor",
        "not",
        "rarely",
        "refuted",
        "shouldn't",
        "untrue",
        "wasn't",
        "weren't",
        "without",
        "won't",
        "wouldn't",
    }
)
_MODAL_TERMS = frozenset(
    {"can", "could", "may", "might", "must", "shall", "should", "will", "would"}
)
_ATTRIBUTION_TERMS = frozenset(
    {
        "according",
        "allegedly",
        "apparently",
        "claimed",
        "purportedly",
        "reportedly",
        "rumor",
        "said",
        "says",
        "state",
        "stated",
        "states",
    }
)
_CONDITION_TERMS = frozenset(
    {
        "assuming",
        "after",
        "before",
        "conditioned",
        "contingent",
        "depending",
        "except",
        "event",
        "if",
        "once",
        "only",
        "otherwise",
        "pending",
        "provided",
        "providing",
        "subject",
        "unless",
        "until",
        "when",
        "whenever",
        "whether",
    }
)
_APPROXIMATION_BOUND_TERMS = frozenset(
    {
        "about",
        "above",
        "almost",
        "approx",
        "approximately",
        "around",
        "below",
        "between",
        "circa",
        "estimated",
        "estimate",
        "estimates",
        "fewer",
        "greater",
        "least",
        "less",
        "floor",
        "maximum",
        "max",
        "minimum",
        "more",
        "most",
        "much",
        "nearly",
        "over",
        "possible",
        "possibly",
        "range",
        "ranged",
        "roughly",
        "under",
        "unlikely",
        "up",
    }
)
_ALWAYS_MATERIAL_TERMS = (
    _NEGATION_TERMS
    | _MODAL_TERMS
    | _ATTRIBUTION_TERMS
    | _CONDITION_TERMS
    | _APPROXIMATION_BOUND_TERMS
)
_SEMANTIC_TERMS = (
    _RELATION_TERMS | _MATERIAL_PREPOSITION_TERMS | _ALWAYS_MATERIAL_TERMS
)
_SCOPE_BOUNDARY = re.compile(
    r"(?<=[.!?])\s+|\n+|;\s*|"
    r"(?:,|:)\s+(?=[A-Z][^\s,.;:!?]*(?:\s|$))|"
    r",\s+(?=(?i:although|but|compared\s+with|however|versus|whereas|while)\b)|"
    r"\s+(?=(?i:although|but|whereas|while)\b)",
)
_ABBREVIATION_END = re.compile(
    r"(?:\b(?:co|corp|dr|etc|inc|ltd|mr|mrs|ms|prof|vs)\."
    r"|(?:\b[A-Za-z]\.){2,})$",
    re.IGNORECASE,
)
_MONTH = (
    r"(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December|Jan\.?|Feb\.?|Mar\.?|Apr\.?|Jun\.?|"
    r"Jul\.?|Aug\.?|Sep\.?|Sept\.?|Oct\.?|Nov\.?|Dec\.?)"
)
_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_UNIT = (
    r"(?:%|percent(?:age)?(?:\s+points?)?|basis\s+points?|bps|thousand|"
    r"million|billion|trillion|USD|EUR|GBP|JPY|CNY|RMB|Australian|Canadian|"
    r"dollars?|euros?|"
    r"pounds?|yen|yuan)"
)
_UNIT_SEQUENCE = rf"{_UNIT}(?:\s+{_UNIT}){{0,2}}"
_CURRENCY = r"(?:US\$|[$€£¥]|USD|EUR|GBP|JPY|CNY|RMB)"
_SIGN = r"[+\-−－]"
_DATE_PATTERNS = (
    re.compile(rf"\b{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,\s*|\s+)\d{{4}}\b"),
    re.compile(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b"),
    re.compile(r"\b(?:FY|fiscal\s+year)\s*[-:]?\s*\d{2,4}\b", re.IGNORECASE),
)
_CURRENCY_QUANTITY = (
    rf"(?:{_SIGN}?{_CURRENCY}\s*{_NUMBER}|{_CURRENCY}\s*{_SIGN}{_NUMBER})"
    rf"(?:\s*{_UNIT_SEQUENCE})?"
)
_UNIT_QUANTITY = rf"{_SIGN}?{_NUMBER}\s*{_UNIT_SEQUENCE}"
_QUANTITY_CORE = rf"(?:{_CURRENCY_QUANTITY}|{_UNIT_QUANTITY})"
_QUANTIFIED = re.compile(
    rf"(?<!\w)(?:\(\s*{_QUANTITY_CORE}\s*\)|{_QUANTITY_CORE}{_SIGN}?)(?!\w)"
)
_NUMBER_ONLY = re.compile(rf"(?<![\w.,]){_SIGN}?{_NUMBER}(?![\w.,])")
_QUALIFIER_ONLY = re.compile(
    rf"(?<!\w)(?:{_CURRENCY}|{_UNIT})(?!\w)"
)
_NUMBER_VALUE = re.compile(_NUMBER)
_FISCAL_YEAR = re.compile(r"\bfiscal\s+year\s+(?P<year>\d{4})\b", re.IGNORECASE)
_INFERENCE_COMPARISON = re.compile(
    r"^(?P<measure>.+?)\s+for\s+(?P<subject>.+?)\s+"
    r"(?P<direction>increased|decreased|unchanged)\s+"
    r"from\s+fiscal\s+year\s+(?P<from_year>\d{4})\s+"
    r"to\s+fiscal\s+year\s+(?P<to_year>\d{4})\.?$",
    re.IGNORECASE,
)
_OBSERVATION_LINK_LIMIT = 96
_QUANTITY_RANGE = re.compile(
    rf"(?:\bbetween\s+{_QUANTITY_CORE}\s+and\s+{_QUANTITY_CORE}\b|"
    rf"\bfrom\s+{_QUANTITY_CORE}\s+to\s+{_QUANTITY_CORE}\b|"
    rf"{_QUANTITY_CORE}\s*(?:-|–|—|\bto\b)\s*{_QUANTITY_CORE})",
    re.IGNORECASE,
)
_BARE_NUMBER_RANGE = re.compile(
    rf"(?<!\w){_SIGN}?{_NUMBER}\s*(?:-|–|—|\bto\b)\s*"
    rf"{_SIGN}?{_NUMBER}\s*{_UNIT_SEQUENCE}(?!\w)",
    re.IGNORECASE,
)
_BOUND_SYMBOL = re.compile(r"(?<!\w)[~≈±≤≥<>]")
_AMBIGUOUS_QUANTITY_PAIR = re.compile(
    rf"{_QUANTITY_CORE}\s*(?:/|\band\b|\bor\b)\s*{_QUANTITY_CORE}",
    re.IGNORECASE,
)
_TRAILING_PLUS_BOUND = re.compile(rf"{_QUANTITY_CORE}\s*\+(?!\w)")
_PARENTHETICAL_QUANTITY = re.compile(
    rf"[（(][^（）()]*{_QUANTITY_CORE}[^（）()]*[）)]",
    re.IGNORECASE,
)
_LOCAL_OBSERVATION_BOUNDARY = re.compile(
    r"[,;:/()（）]\s*|[—–]\s*|\b(?:although|and|but|or|whereas|while)\b",
    re.IGNORECASE,
)
_SAFE_LEADING_DATE = re.compile(
    rf"^\s*(?:on|as\s+of)\s+(?:{_MONTH}\s+\d{{1,2}}"
    rf"(?:st|nd|rd|th)?(?:,\s*|\s+)\d{{4}}|\d{{4}}[-/]\d{{1,2}}[-/]\d{{1,2}})"
    r"\s*[,:—–-]?\s*$",
    re.IGNORECASE,
)
_SAFE_TRAILING_DETAIL = re.compile(
    r"^\s*(?:"
    r"for\s+fiscal\s+year\s+\d{4}|"
    r"through\s+(?:the\s+)?(?:combined\s+)?(?:illustrative\s+)?"
    r"mechanisms?\s+of\s+[\w\s,\-]+"
    r")\s*[.!?]?\s*$",
    re.IGNORECASE,
)
_CONFLICT_VALUE_PREDICATES = frozenset(
    {
        "are",
        "equals",
        "identifies",
        "is",
        "lists",
        "operates",
        "reports",
        "states",
        "was",
        "were",
    }
)


class UnsafeModelOutput(ValueError):
    """The model response cannot be safely converted to a grounded answer."""


@dataclass(frozen=True, slots=True)
class _AuthoritativeScope:
    start: int
    end: int
    text: str


@dataclass(frozen=True, slots=True)
class _EvidenceExcerpt:
    citation_id: str
    quote: str
    scopes: tuple[_AuthoritativeScope, ...]


@dataclass(frozen=True, slots=True)
class _ValidatedClaim:
    claim: Claim
    evidence: tuple[_EvidenceExcerpt, ...]


@dataclass(frozen=True, slots=True)
class _Literal:
    kind: str
    value: str


@dataclass(frozen=True, slots=True)
class _LiteralSpan:
    literal: _Literal
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class _Observation:
    year: int
    value: Decimal
    qualifier: str
    accounting: bool
    local_text: str
    binding_text: str


def _reject(message: str) -> None:
    raise UnsafeModelOutput(message)


def _mapping(value: object, expected: frozenset[str], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _reject(f"{name} must be an object")
    keys = set(value)
    if keys != expected:
        _reject(f"{name} has missing or unknown fields")
    return value


def _sequence(value: object, name: str) -> Sequence[object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        _reject(f"{name} must be an array")
    return value


def _model_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        _reject(f"{name} must be non-empty text")
    return value.strip()


def _find_literal_spans(text: str) -> tuple[_LiteralSpan, ...]:
    occupied: list[tuple[int, int]] = []
    literals: list[_LiteralSpan] = []

    def add(pattern: re.Pattern[str], kind: str) -> None:
        for match in pattern.finditer(text):
            start, end = match.span()
            if any(start < used_end and end > used_start for used_start, used_end in occupied):
                continue
            occupied.append((start, end))
            literals.append(_LiteralSpan(_Literal(kind, match.group(0)), start, end))

    for pattern in _DATE_PATTERNS:
        add(pattern, "date")
    add(_QUANTIFIED, "quantity")
    add(_NUMBER_ONLY, "number")
    add(_QUALIFIER_ONLY, "qualifier")
    return tuple(sorted(literals, key=lambda item: item.start))


def _find_literals(text: str) -> tuple[_Literal, ...]:
    return tuple(item.literal for item in _find_literal_spans(text))


def _raw_token_matches(text: str) -> tuple[re.Match[str], ...]:
    return tuple(_TOKEN.finditer(text))


def _canonical_token(token: str) -> str:
    return token.casefold().replace("’", "'")


def _raw_token_values(text: str) -> tuple[str, ...]:
    return tuple(_canonical_token(match.group(0)) for match in _raw_token_matches(text))


def _content_token_sequence(text: str) -> tuple[str, ...]:
    return tuple(
        _canonical_token(token)
        for token in _TOKEN.findall(text)
        if _canonical_token(token) not in _STOPWORDS
    )


def _content_tokens(text: str) -> set[str]:
    return set(_content_token_sequence(text))


def _ordered_subsequence_positions(
    needle: tuple[str, ...],
    haystack: tuple[str, ...],
) -> tuple[int, ...] | None:
    if not needle:
        return None
    position = 0
    matched: list[int] = []
    for index, token in enumerate(haystack):
        if token == needle[position]:
            matched.append(index)
            position += 1
            if position == len(needle):
                return tuple(matched)
    return None


def _contains_contiguous(
    needle: tuple[str, ...],
    haystack: tuple[str, ...],
) -> bool:
    return bool(needle) and any(
        haystack[index : index + len(needle)] == needle
        for index in range(len(haystack) - len(needle) + 1)
    )


def _scope_spans(text: str) -> tuple[_AuthoritativeScope, ...]:
    scopes: list[_AuthoritativeScope] = []
    start = 0
    for boundary in _SCOPE_BOUNDARY.finditer(text):
        fragment = text[start : boundary.start()].rstrip()
        fragment_tokens = set(_raw_token_values(fragment))
        next_character = text[boundary.end() :].lstrip()[:1]
        if boundary.group(0)[:1] in {",", ":"} and next_character.isupper():
            # A leading qualifier and a new sentence-like clause are
            # indistinguishable at this punctuation boundary. Keep them in one
            # authoritative scope and fail closed rather than dropping the
            # possible qualifier.
            continue
        if boundary.group(0)[:1] in {",", ":"} and (
            fragment_tokens & _ALWAYS_MATERIAL_TERMS
        ):
            continue
        if (
            boundary.start() > 0
            and text[boundary.start() - 1] == "."
            and "\n" not in boundary.group(0)
            and _ABBREVIATION_END.search(text[: boundary.start()])
        ):
            continue
        end = boundary.start()
        if start < end and text[start:end].strip():
            scopes.append(_AuthoritativeScope(start, end, text[start:end].strip()))
        start = boundary.end()
    if start < len(text) and text[start:].strip():
        scopes.append(_AuthoritativeScope(start, len(text), text[start:].strip()))
    return tuple(scopes)


def _occurrences(text: str, value: str) -> tuple[int, ...]:
    positions: list[int] = []
    start = 0
    while True:
        position = text.find(value, start)
        if position < 0:
            return tuple(positions)
        positions.append(position)
        start = position + 1


def _authoritative_scopes(text: str, quote: str) -> tuple[_AuthoritativeScope, ...]:
    occurrences = _occurrences(text, quote)
    if len(occurrences) != 1:
        _reject("evidence quote must occur exactly once in its cited Chunk")
    quote_start = occurrences[0]
    quote_end = quote_start + len(quote)
    scopes = tuple(
        scope
        for scope in _scope_spans(text)
        if quote_start < scope.end and quote_end > scope.start
    )
    if not scopes:
        _reject("evidence quote does not overlap an authoritative source scope")
    return scopes


def _scope_match_positions(
    claim_tokens: tuple[str, ...],
    scope: _AuthoritativeScope,
) -> tuple[tuple[re.Match[str], ...], tuple[int, ...]] | None:
    raw_matches = _raw_token_matches(scope.text)
    content: list[tuple[int, str]] = [
        (index, _canonical_token(match.group(0)))
        for index, match in enumerate(raw_matches)
        if _canonical_token(match.group(0)) not in _STOPWORDS
    ]
    matched_content = _ordered_subsequence_positions(
        claim_tokens,
        tuple(token for _, token in content),
    )
    if matched_content is None:
        return None
    return raw_matches, tuple(content[index][0] for index in matched_content)


def _semantic_signature(
    tokens: tuple[str, ...],
    *,
    matched_start: int,
    matched_end: int,
    source_scope: bool,
) -> tuple[str, ...]:
    return tuple(
        token
        for index, token in enumerate(tokens)
        if token in _ALWAYS_MATERIAL_TERMS
        or (
            token in (_RELATION_TERMS | _MATERIAL_PREPOSITION_TERMS)
            and (
                not source_scope
                or matched_start <= index <= matched_end
            )
        )
    )


def _scope_edge_omissions_are_safe(
    claim_text: str,
    scope: _AuthoritativeScope,
    raw_matches: tuple[re.Match[str], ...],
    matched_indexes: tuple[int, ...],
) -> bool:
    """Permit only audited, independently non-qualifying scope edge details."""
    prefix = scope.text[: raw_matches[matched_indexes[0]].start()]
    suffix = scope.text[raw_matches[matched_indexes[-1]].end() :]
    claim_matches = _raw_token_matches(claim_text)
    claim_content_indexes = tuple(
        index
        for index, match in enumerate(claim_matches)
        if _canonical_token(match.group(0)) not in _STOPWORDS
    )
    if not claim_content_indexes:
        return False
    claim_prefix = claim_text[: claim_matches[claim_content_indexes[0]].start()]
    claim_suffix = claim_text[claim_matches[claim_content_indexes[-1]].end() :]
    prefix_matches = _raw_token_values(prefix)
    claim_prefix_matches = _raw_token_values(claim_prefix)
    suffix_matches = _raw_token_values(suffix)
    claim_suffix_matches = _raw_token_values(claim_suffix)
    if prefix_matches != claim_prefix_matches and not (
        not claim_prefix_matches and _SAFE_LEADING_DATE.fullmatch(prefix) is not None
    ):
        return False
    if suffix_matches != claim_suffix_matches and not (
        not claim_suffix_matches and _SAFE_TRAILING_DETAIL.fullmatch(suffix) is not None
    ):
        return False
    return True


def _literal_anchor(
    text: str,
    literal: _LiteralSpan,
    *,
    before: bool,
) -> tuple[str, ...]:
    literals = _find_literal_spans(text)
    candidates = tuple(
        match
        for match in _raw_token_matches(text)
        if _canonical_token(match.group(0)) not in _STOPWORDS
        and not any(
            match.start() < item.end and match.end() > item.start
            for item in literals
        )
        and (
            match.end() <= literal.start
            if before
            else match.start() >= literal.end
        )
    )
    if not candidates:
        return ()
    matches = candidates[-2:] if before else candidates[:2]
    return tuple(_canonical_token(match.group(0)) for match in matches)


def _literal_is_bound_to_match(
    claim_text: str,
    claim_literal: _LiteralSpan,
    claim_raw_matches: tuple[re.Match[str], ...],
    claim_content_indexes: tuple[int, ...],
    scope: _AuthoritativeScope,
    raw_matches: tuple[re.Match[str], ...],
    matched_indexes: tuple[int, ...],
) -> bool:
    claim_literal_positions = tuple(
        position
        for position, raw_index in enumerate(claim_content_indexes)
        if claim_raw_matches[raw_index].start() < claim_literal.end
        and claim_raw_matches[raw_index].end() > claim_literal.start
    )
    if not claim_literal_positions:
        return False
    mapped_indexes = tuple(matched_indexes[position] for position in claim_literal_positions)
    claim_before = _literal_anchor(claim_text, claim_literal, before=True)
    claim_after = _literal_anchor(claim_text, claim_literal, before=False)
    for source_literal in _find_literal_spans(scope.text):
        if source_literal.literal != claim_literal.literal:
            continue
        if not all(
            raw_matches[index].start() < source_literal.end
            and raw_matches[index].end() > source_literal.start
            for index in mapped_indexes
        ):
            continue
        source_before = _literal_anchor(scope.text, source_literal, before=True)
        source_after = _literal_anchor(scope.text, source_literal, before=False)
        if claim_before:
            if source_before == claim_before:
                return True
            continue
        if claim_after and source_after == claim_after:
            return True
    return False


def _scope_supports_sourced_claim(
    text: str,
    scope: _AuthoritativeScope,
) -> bool:
    claim_tokens = _content_token_sequence(text)
    matched = _scope_match_positions(claim_tokens, scope)
    if matched is None:
        return False
    raw_matches, matched_indexes = matched
    if not _scope_edge_omissions_are_safe(text, scope, raw_matches, matched_indexes):
        return False
    source_content_indexes = tuple(
        index
        for index, match in enumerate(raw_matches)
        if _canonical_token(match.group(0)) not in _STOPWORDS
    )
    matched_content_positions = tuple(
        source_content_indexes.index(index) for index in matched_indexes
    )
    for left, right in zip(matched_content_positions, matched_content_positions[1:]):
        gap = tuple(
            _canonical_token(raw_matches[index].group(0))
            for index in source_content_indexes[left + 1 : right]
        )
        if not gap:
            continue
        header_gap = (
            len(gap) == 5
            and gap[1:3] == ("fiscal", "year")
            and re.fullmatch(r"\d{4}", gap[3]) is not None
            and gap[4] == claim_tokens[0]
        )
        if not header_gap:
            return False
    claim_raw = _raw_token_values(text)
    scope_raw = tuple(_canonical_token(match.group(0)) for match in raw_matches)
    claim_signature = _semantic_signature(
        claim_raw,
        matched_start=0,
        matched_end=max(0, len(claim_raw) - 1),
        source_scope=False,
    )
    scope_signature = _semantic_signature(
        scope_raw,
        matched_start=matched_indexes[0],
        matched_end=matched_indexes[-1],
        source_scope=True,
    )
    if claim_signature != scope_signature:
        return False
    if bool(_QUANTITY_RANGE.search(text) or _BARE_NUMBER_RANGE.search(text)) != bool(
        _QUANTITY_RANGE.search(scope.text) or _BARE_NUMBER_RANGE.search(scope.text)
    ):
        return False
    if bool(_AMBIGUOUS_QUANTITY_PAIR.search(text)) != bool(
        _AMBIGUOUS_QUANTITY_PAIR.search(scope.text)
    ):
        return False
    if bool(_TRAILING_PLUS_BOUND.search(text)) != bool(
        _TRAILING_PLUS_BOUND.search(scope.text)
    ):
        return False
    if tuple(_BOUND_SYMBOL.findall(text)) != tuple(_BOUND_SYMBOL.findall(scope.text)):
        return False
    claim_raw_matches = _raw_token_matches(text)
    claim_content_indexes = tuple(
        index
        for index, match in enumerate(claim_raw_matches)
        if _canonical_token(match.group(0)) not in _STOPWORDS
    )
    return all(
        _literal_is_bound_to_match(
            text,
            literal,
            claim_raw_matches,
            claim_content_indexes,
            scope,
            raw_matches,
            matched_indexes,
        )
        for literal in _find_literal_spans(text)
    )


def _quantity_parts(literal: str) -> tuple[Decimal, str, bool] | None:
    match = _NUMBER_VALUE.search(literal)
    if match is None:
        return None
    try:
        value = Decimal(match.group(0).replace(",", ""))
    except InvalidOperation:
        return None
    stripped = literal.strip()
    accounting = stripped.startswith("(") and stripped.endswith(")")
    sign_prefix = stripped[: stripped.find(match.group(0))]
    if (
        accounting
        or "-" in sign_prefix
        or "−" in sign_prefix
        or "－" in sign_prefix
        or stripped.endswith(("-", "−", "－"))
    ):
        value = -value
    qualifier = f"{literal[:match.start()]}<number>{literal[match.end():]}"
    qualifier = re.sub(r"[()+−－-]", "", qualifier)
    qualifier = re.sub(r"\s+", " ", qualifier).strip().casefold()
    return value, qualifier, accounting


def _linked_year(
    quote: str,
    quantity: re.Match[str],
    quantities: tuple[re.Match[str], ...],
    years: tuple[re.Match[str], ...],
) -> re.Match[str] | None:
    next_quantity = min(
        (item.start() for item in quantities if item.start() > quantity.start()),
        default=len(quote),
    )
    following = [
        year
        for year in years
        if quantity.end() <= year.start() < next_quantity
        and year.start() - quantity.end() <= _OBSERVATION_LINK_LIMIT
        and re.search(r"[.;!?]", quote[quantity.end() : year.start()]) is None
    ]
    previous_quantities = tuple(
        item for item in quantities if item.end() < quantity.start()
    )
    preceding = (
        []
        if previous_quantities
        else [
            year
            for year in years
            if year.end() <= quantity.start()
            and quantity.start() - year.end() <= _OBSERVATION_LINK_LIMIT
            and re.search(r"[.;!?]", quote[year.end() : quantity.start()]) is None
        ]
    )
    candidates = tuple(preceding + following)
    if len({int(item.group("year")) for item in candidates}) != 1:
        return None
    return following[0] if following else preceding[-1]


def _observations(scope: _AuthoritativeScope) -> tuple[_Observation, ...]:
    quantities = tuple(_QUANTIFIED.finditer(scope.text))
    years = tuple(_FISCAL_YEAR.finditer(scope.text))
    candidates: list[
        tuple[re.Match[str], re.Match[str], Decimal, str, bool]
    ] = []
    for quantity in quantities:
        year = _linked_year(scope.text, quantity, quantities, years)
        parts = _quantity_parts(quantity.group(0))
        if year is None or parts is None:
            continue
        value, qualifier, accounting = parts
        candidates.append((quantity, year, value, qualifier, accounting))

    observations: list[_Observation] = []
    extents = tuple(
        (min(quantity.start(), year.start()), max(quantity.end(), year.end()))
        for quantity, year, *_ in candidates
    )
    for index, (quantity, year, value, qualifier, accounting) in enumerate(candidates):
        observation_start, observation_end = extents[index]
        local_start = 0
        if index:
            previous_end = extents[index - 1][1]
            left_boundaries = tuple(
                _LOCAL_OBSERVATION_BOUNDARY.finditer(
                    scope.text,
                    previous_end,
                    observation_start,
                )
            )
            local_start = (
                left_boundaries[-1].end() if left_boundaries else previous_end
            )
        local_end = len(scope.text)
        if index + 1 < len(extents):
            next_start = extents[index + 1][0]
            right_boundary = _LOCAL_OBSERVATION_BOUNDARY.search(
                scope.text,
                observation_end,
                next_start,
            )
            local_end = right_boundary.start() if right_boundary else next_start
        local_text = scope.text[local_start:local_end].strip()
        binding_boundaries = tuple(
            _LOCAL_OBSERVATION_BOUNDARY.finditer(
                scope.text,
                local_start,
                quantity.start(),
            )
        )
        binding_start = (
            binding_boundaries[-1].end() if binding_boundaries else local_start
        )
        binding_text = scope.text[binding_start : quantity.start()].strip()
        observation = _Observation(
            int(year.group("year")),
            value,
            qualifier,
            accounting,
            local_text,
            binding_text,
        )
        if observation not in observations:
            observations.append(observation)
    return tuple(observations)


def _text_is_uncertain_or_bounded(text: str) -> bool:
    tokens = set(_raw_token_values(text))
    return bool(tokens & _APPROXIMATION_BOUND_TERMS) or bool(
        _QUANTITY_RANGE.search(text)
        or _BARE_NUMBER_RANGE.search(text)
        or _AMBIGUOUS_QUANTITY_PAIR.search(text)
        or _TRAILING_PLUS_BOUND.search(text)
        or _PARENTHETICAL_QUANTITY.search(text)
        or _BOUND_SYMBOL.search(text)
    )


def _observation_binds(
    observation: _Observation,
    measure_tokens: tuple[str, ...],
    subject_tokens: tuple[str, ...],
) -> bool:
    local_tokens = _content_token_sequence(observation.local_text)
    binding_tokens = _content_token_sequence(observation.binding_text)
    if not _contains_contiguous(subject_tokens, local_tokens):
        return False
    if not _contains_contiguous(measure_tokens, binding_tokens):
        return False
    measure_positions = tuple(
        index
        for index in range(len(binding_tokens) - len(measure_tokens) + 1)
        if binding_tokens[index : index + len(measure_tokens)] == measure_tokens
    )
    if not measure_positions:
        return False
    last_measure_end = measure_positions[-1] + len(measure_tokens)
    if len(binding_tokens) - last_measure_end > 2:
        return False
    if set(subject_tokens) & set(binding_tokens):
        return True
    capitalized = {
        _canonical_token(match.group(0))
        for match in _raw_token_matches(observation.binding_text)
        if match.group(0)[:1].isupper()
    }
    return not capitalized


def _validate_sourced_wording(
    text: str,
    evidence: tuple[_EvidenceExcerpt, ...],
) -> None:
    claim_tokens = _content_token_sequence(text)
    if not claim_tokens:
        _reject("a sourced claim must contain evidence-bearing terms")
    for citation_id in {item.citation_id for item in evidence}:
        scopes = tuple(
            scope
            for item in evidence
            if item.citation_id == citation_id
            for scope in item.scopes
        )
        if not any(_scope_supports_sourced_claim(text, scope) for scope in scopes):
            _reject(
                "each sourced citation must support ordered wording, material semantics, "
                "and exact literals inside one authoritative source scope"
            )


def _validate_inference_wording(
    text: str,
    evidence: tuple[_EvidenceExcerpt, ...],
) -> None:
    comparison = _INFERENCE_COMPARISON.fullmatch(text)
    if comparison is None:
        _reject("an inference must use the audited fiscal-year comparison form")
    measure = comparison.group("measure")
    subject = comparison.group("subject")
    if _find_literals(measure) or _find_literals(subject):
        _reject("an inference measure and subject must not contain literals")
    measure_tokens = _content_token_sequence(measure)
    subject_tokens = _content_token_sequence(subject)
    if not measure_tokens or not subject_tokens:
        _reject("an inference requires an evidence-bearing measure and subject")
    from_year = int(comparison.group("from_year"))
    to_year = int(comparison.group("to_year"))
    if from_year >= to_year:
        _reject("an inference fiscal years must be strictly increasing")

    requested_years = {from_year, to_year}
    relevant: list[_Observation] = []
    for excerpt in evidence:
        excerpt_observations: list[_Observation] = []
        for scope in excerpt.scopes:
            observations = tuple(
                item
                for item in _observations(scope)
                if item.year in requested_years
                and _observation_binds(item, measure_tokens, subject_tokens)
            )
            if not observations:
                continue
            if any(
                item.accounting or _text_is_uncertain_or_bounded(item.local_text)
                for item in observations
            ):
                _reject(
                    "inference observations cannot use accounting parentheses, "
                    "approximations, ranges, or bounds"
                )
            excerpt_observations.extend(observations)
        if not excerpt_observations:
            _reject(
                "every inference excerpt must locally bind measure, subject, quantity, and year"
            )
        relevant.extend(excerpt_observations)

    by_qualifier: dict[str, dict[int, set[Decimal]]] = {}
    for item in relevant:
        by_year = by_qualifier.setdefault(item.qualifier, {})
        by_year.setdefault(item.year, set()).add(item.value)
    complete = [
        (qualifier, by_year)
        for qualifier, by_year in by_qualifier.items()
        if requested_years <= set(by_year)
    ]
    if len(complete) != 1:
        _reject("inference evidence must identify one shared quantity qualifier")
    qualifier, by_year = complete[0]
    if any(item.qualifier != qualifier for item in relevant) or any(
        len(by_year[year]) != 1 for year in requested_years
    ):
        _reject("inference evidence contains ambiguous numeric-year values")

    from_value = next(iter(by_year[from_year]))
    to_value = next(iter(by_year[to_year]))
    actual_direction = (
        "increased"
        if to_value > from_value
        else "decreased"
        if to_value < from_value
        else "unchanged"
    )
    if comparison.group("direction").casefold() != actual_direction:
        _reject("inference direction does not match its cited numeric-year values")


def _conflict_shape(
    text: str,
) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[tuple[str, str, bool], ...],
    tuple[Decimal, ...],
]:
    """Return a literalized statement skeleton plus comparable value metadata."""
    literals = _find_literal_spans(text)
    emitted: set[int] = set()
    tokens: list[str] = []
    for match in _raw_token_matches(text):
        overlaps = tuple(
            index
            for index, literal in enumerate(literals)
            if match.start() < literal.end and match.end() > literal.start
        )
        if not overlaps:
            tokens.append(_canonical_token(match.group(0)))
            continue
        literal_index = overlaps[0]
        if literal_index not in emitted:
            tokens.append(f"<{literals[literal_index].literal.kind}>")
            emitted.add(literal_index)

    dates: list[str] = []
    formats: list[tuple[str, str, bool]] = []
    values: list[Decimal] = []
    for literal in literals:
        if literal.literal.kind == "date":
            dates.append(
                re.sub(r"\s+", " ", literal.literal.value).strip().casefold()
            )
            continue
        if literal.literal.kind not in {"number", "quantity"}:
            _reject("conflict values must use an auditable number or quantity")
        parts = _quantity_parts(literal.literal.value)
        if parts is None:
            _reject("conflict contains an unauditable numeric value")
        value, qualifier, accounting = parts
        formats.append((literal.literal.kind, qualifier, accounting))
        values.append(value)
    return tuple(tokens), tuple(dates), tuple(formats), tuple(values)


def _common_prefix_length(values: tuple[tuple[str, ...], ...]) -> int:
    shortest = min(len(value) for value in values)
    for index in range(shortest):
        if len({value[index] for value in values}) != 1:
            return index
    return shortest


def _common_suffix_length(
    values: tuple[tuple[str, ...], ...],
    prefix_length: int,
) -> int:
    available = min(len(value) - prefix_length for value in values)
    for offset in range(1, available + 1):
        if len({value[-offset] for value in values}) != 1:
            return offset - 1
    return available


def _topic_matches_stable_statement(
    topic: str,
    stable_tokens: tuple[str, ...],
) -> bool:
    topic_tokens = _content_token_sequence(topic)
    stable_content = tuple(
        token
        for token in stable_tokens
        if token not in _STOPWORDS and not token.startswith("<")
    )
    return _contains_contiguous(topic_tokens, stable_content)


def _validate_conflict_incompatibility(
    topic: str,
    claims: tuple[Claim, ...],
) -> None:
    """Fail closed unless alternatives concern one scope and cannot all be true."""
    shaped = tuple(_conflict_shape(claim.text) for claim in claims)
    skeletons = tuple(item[0] for item in shaped)
    dates = tuple(item[1] for item in shaped)
    formats = tuple(item[2] for item in shaped)
    values = tuple(item[3] for item in shaped)
    if len(set(dates)) != 1:
        _reject("conflict alternatives must use the same explicit period")

    if any(values):
        if (
            not all(values)
            or len(set(skeletons)) != 1
            or len(set(formats)) != 1
            or len(set(values)) != len(values)
        ):
            _reject(
                "numeric conflict alternatives must share subject, measure, period, "
                "and comparable units while disagreeing on value"
            )
        stable = tuple(
            token for token in skeletons[0] if not token.startswith("<")
        )
        if not _topic_matches_stable_statement(topic, stable):
            _reject("conflict topic must name the shared subject or measure")
        return

    prefix_length = _common_prefix_length(skeletons)
    suffix_length = _common_suffix_length(skeletons, prefix_length)
    prefix = skeletons[0][:prefix_length]
    suffix = skeletons[0][len(skeletons[0]) - suffix_length :] if suffix_length else ()
    middles = tuple(
        value[prefix_length : len(value) - suffix_length if suffix_length else None]
        for value in skeletons
    )
    if (
        len(prefix) < 2
        or prefix[-1] not in _CONFLICT_VALUE_PREDICATES
        or any(not middle for middle in middles)
        or len(set(middles)) != len(middles)
    ):
        _reject(
            "text conflict alternatives must share one subject/measure predicate "
            "and disagree only on its value"
        )
    if not _topic_matches_stable_statement(topic, prefix + suffix):
        _reject("conflict topic must name the shared subject or measure")


def _citation_ids(
    value: object,
    context: Mapping[str, LabelledContext],
    limits: GenerationLimits,
) -> tuple[str, ...]:
    values = _sequence(value, "citation_ids")
    if len(values) > limits.max_citations_per_claim:
        _reject("claim citations exceed max_citations_per_claim")
    identifiers = tuple(_model_text(item, "citation_id") for item in values)
    if not identifiers:
        _reject("every material claim requires a citation")
    if len(identifiers) != len(set(identifiers)):
        _reject("citation_ids must be unique")
    if any(identifier not in context for identifier in identifiers):
        _reject("model returned an unknown citation label")
    return identifiers


def _evidence(
    value: object,
    citation_ids: tuple[str, ...],
    context: Mapping[str, LabelledContext],
    limits: GenerationLimits,
) -> tuple[_EvidenceExcerpt, ...]:
    values = _sequence(value, "evidence")
    excerpts: list[_EvidenceExcerpt] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(values):
        record = _mapping(item, _EXPECTED_EVIDENCE, f"evidence[{index}]")
        citation_id = _model_text(record["citation_id"], "evidence citation_id")
        quote = _model_text(record["quote"], "evidence quote")
        if len(quote) > limits.max_evidence_quote_chars:
            _reject("evidence quote exceeds max_evidence_quote_chars")
        if citation_id not in citation_ids:
            _reject("evidence references a citation not attached to the claim")
        scopes = _authoritative_scopes(context[citation_id].chunk.text, quote)
        key = (citation_id, quote)
        if key in seen:
            _reject("evidence excerpts must be unique")
        seen.add(key)
        excerpts.append(_EvidenceExcerpt(citation_id, quote, scopes))
    if {item.citation_id for item in excerpts} != set(citation_ids):
        _reject("every claim citation requires an exact evidence excerpt")
    return tuple(excerpts)


def _claim(
    payload: object,
    context: Mapping[str, LabelledContext],
    limits: GenerationLimits,
    *,
    alternative: bool = False,
) -> _ValidatedClaim:
    expected = _EXPECTED_ALTERNATIVE if alternative else _EXPECTED_CLAIM
    record = _mapping(payload, expected, "conflict alternative" if alternative else "claim")
    text = _model_text(record["text"], "claim text")
    if len(text) > limits.max_claim_chars:
        _reject("claim text exceeds max_claim_chars")
    if _INLINE_LABEL.search(text):
        _reject("claim text must not contain model-authored citation labels")
    identifiers = _citation_ids(record["citation_ids"], context, limits)
    raw_evidence = _sequence(record["evidence"], "evidence")
    if len(raw_evidence) > limits.max_evidence_quotes:
        _reject("claim evidence exceeds max_evidence_quotes")
    evidence = _evidence(raw_evidence, identifiers, context, limits)
    if alternative:
        material = True
        inference = False
    else:
        if type(record["material"]) is not bool or record["material"] is not True:
            _reject("all returned claims must be explicitly material")
        if type(record["inference"]) is not bool:
            _reject("claim inference must be boolean")
        material = True
        inference = bool(record["inference"])
    if inference:
        _validate_inference_wording(text, evidence)
    else:
        _validate_sourced_wording(text, evidence)
    return _ValidatedClaim(Claim(text, material, identifiers, inference), evidence)


def _require_unique_claims(claims: tuple[Claim, ...]) -> None:
    def canonical_text(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value).casefold()
        normalized = "".join(
            character
            for character in normalized
            if unicodedata.category(character) != "Cf"
        )
        normalized = re.sub(r"\s+", " ", normalized).strip()
        normalized = re.sub(r"\s+([,.;:!?])", r"\1", normalized)
        return normalized.rstrip(".,;:!?").rstrip()

    keys = tuple(
        (
            claim.inference,
            canonical_text(claim.text),
        )
        for claim in claims
    )
    if len(keys) != len(set(keys)):
        _reject("model output contains duplicate canonical claims")


def _render_claim(claim: Claim) -> str:
    prefix = "Inference: " if claim.inference else ""
    markers = " ".join(f"[{citation_id}]" for citation_id in claim.citation_ids)
    return f"{prefix}{claim.text} {markers}"


def _selected_citations(
    claims: tuple[Claim, ...],
    labelled: tuple[LabelledContext, ...],
) -> tuple[AnswerCitation, ...]:
    referenced = {identifier for claim in claims for identifier in claim.citation_ids}
    return tuple(
        AnswerCitation.from_retrieval(item.citation_id, item.chunk.citation)
        for item in labelled
        if item.citation_id in referenced
    )


class GroundedGenerationService:
    """Convert untrusted structured model output into a server-rendered answer."""

    def __init__(self, model: AnswerModel) -> None:
        self.model = model

    def generate(self, request: GenerationRequest) -> AnswerResult:
        if not request.chunks:
            return AnswerResult.refusal()
        if (
            len(request.question) > request.limits.max_question_chars
            or len(request.chunks) > request.limits.max_context_chunks
            or sum(len(chunk.text) for chunk in request.chunks)
            > request.limits.max_context_chars
        ):
            return AnswerResult.refusal(failure_code=GENERATION_LIMIT_EXCEEDED)
        labelled = label_context(request.chunks)
        try:
            for item in labelled:
                citation = AnswerCitation.from_retrieval(
                    item.citation_id,
                    item.chunk.citation,
                )
                if not item.chunk.text:
                    raise ValueError("retrieval context contains empty Chunk text")
                if len(item.chunk.text) != citation.char_end - citation.char_start:
                    raise ValueError("Chunk text does not match its citation range")
                checksum = hashlib.sha256(item.chunk.text.encode("utf-8")).hexdigest()
                if checksum != citation.chunk_checksum:
                    raise ValueError("Chunk text does not match its citation checksum")
        except (TypeError, ValueError):
            return AnswerResult.refusal(failure_code=INVALID_CONTEXT)
        context = {item.citation_id: item for item in labelled}
        prompt = build_prompt(request.question, labelled)
        if len(prompt) > request.limits.max_prompt_chars:
            return AnswerResult.refusal(failure_code=GENERATION_LIMIT_EXCEEDED)
        payload = self.model.generate(AnswerModelRequest(prompt=prompt))
        try:
            return self._validated_result(
                payload,
                labelled,
                context,
                request.limits,
            )
        except UnsafeModelOutput:
            return AnswerResult.refusal(failure_code=INVALID_MODEL_OUTPUT)

    @staticmethod
    def _validated_result(
        payload: object,
        labelled: tuple[LabelledContext, ...],
        context: Mapping[str, LabelledContext],
        limits: GenerationLimits,
    ) -> AnswerResult:
        record = _mapping(payload, _EXPECTED_TOP_LEVEL, "model output")
        raw_status = _model_text(record["status"], "status")
        try:
            status = AnswerStatus(raw_status)
        except ValueError:
            _reject("model output has an unknown status")
        raw_claims = _sequence(record["claims"], "claims")
        raw_conflicts = _sequence(record["conflicts"], "conflicts")

        if status is AnswerStatus.INSUFFICIENT_CONTEXT:
            if raw_claims or raw_conflicts:
                _reject("insufficient_context cannot contain claims or conflicts")
            return AnswerResult.refusal()

        if status is AnswerStatus.ANSWERED:
            if not raw_claims or raw_conflicts:
                _reject("answered requires claims and forbids conflicts")
            if len(raw_claims) > limits.max_claims:
                _reject("answered claims exceed max_claims")
            validated = tuple(_claim(item, context, limits) for item in raw_claims)
            claims = tuple(item.claim for item in validated)
            _require_unique_claims(claims)
            answer = "\n".join(_render_claim(claim) for claim in claims)
            return AnswerResult(
                status=status,
                answer=answer,
                claims=claims,
                citations=_selected_citations(claims, labelled),
            )

        if raw_claims or not raw_conflicts:
            _reject("conflict requires conflicts and forbids top-level claims")
        claims_list: list[Claim] = []
        conflicts: list[Conflict] = []
        if len(raw_conflicts) > limits.max_claims:
            _reject("conflicts exceed max_claims")
        for conflict_index, raw_conflict in enumerate(raw_conflicts):
            conflict_record = _mapping(
                raw_conflict,
                _EXPECTED_CONFLICT,
                f"conflicts[{conflict_index}]",
            )
            topic = _model_text(conflict_record["topic"], "conflict topic")
            if len(topic) > limits.max_claim_chars:
                _reject("conflict topic exceeds max_claim_chars")
            if _INLINE_LABEL.search(topic) or _find_literals(topic):
                _reject("conflict topic must not contain claims or citation labels")
            raw_alternatives = _sequence(
                conflict_record["alternatives"],
                "conflict alternatives",
            )
            if len(raw_alternatives) < 2:
                _reject("a conflict requires at least two alternatives")
            if len(claims_list) + len(raw_alternatives) > limits.max_claims:
                _reject("conflict alternatives exceed max_claims")
            alternatives = tuple(
                _claim(item, context, limits, alternative=True)
                for item in raw_alternatives
            )
            alternative_claims = tuple(item.claim for item in alternatives)
            if len({claim.text for claim in alternative_claims}) != len(alternative_claims):
                _reject("conflict alternatives must be distinct")
            _validate_conflict_incompatibility(topic, alternative_claims)
            provenance_sets = tuple(
                {
                    (
                        context[citation_id].chunk.citation.document_id,
                        context[citation_id].chunk.citation.version_id,
                    )
                    for citation_id in claim.citation_ids
                }
                for claim in alternative_claims
            )
            if len(set(frozenset(value) for value in provenance_sets)) < 2:
                _reject("conflict alternatives must cite distinct source provenance")
            if len(set().union(*provenance_sets)) < 2:
                _reject("a conflict requires two different Document/Version sources")
            start = len(claims_list)
            claims_list.extend(alternative_claims)
            conflicts.append(
                Conflict(topic, tuple(range(start, start + len(alternative_claims))))
            )
        claims = tuple(claims_list)
        _require_unique_claims(claims)
        answer = "Conflicting source statements:\n" + "\n".join(
            _render_claim(claim) for claim in claims
        )
        return AnswerResult(
            status=AnswerStatus.CONFLICT,
            answer=answer,
            claims=claims,
            citations=_selected_citations(claims, labelled),
            conflicts=tuple(conflicts),
        )
