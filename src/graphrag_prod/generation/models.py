"""Immutable contracts for cited, fail-closed answer generation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import re
from enum import StrEnum
from typing import Any, Protocol

from graphrag_prod.retrieval.models import Citation, RetrievedChunk


PROMPT_VERSION = "grounded-answer-v1.3.0"
OUTPUT_SCHEMA_VERSION = "grounded-answer-output-v1.0.0"
REFUSAL_ANSWER = "I don't have enough cited context to answer this question."
_INLINE_CITATION = re.compile(r"\[S[1-9][0-9]*\]")
_MODEL_CITATION_MARKER = re.compile(
    r"\[\s*(?:S\s*\d+|source\b[^\]]*|citation\b[^\]]*)\s*\]",
    re.IGNORECASE,
)
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _text(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be text")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    if "\x00" in normalized:
        raise ValueError(f"{name} must not contain NUL")
    return normalized


class AnswerStatus(StrEnum):
    ANSWERED = "answered"
    INSUFFICIENT_CONTEXT = "insufficient_context"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class GenerationLimits:
    """Independent input/output bounds; callers cannot bypass retrieval limits."""

    max_context_chunks: int = 10
    max_context_chars: int = 20_000
    max_claims: int = 20
    max_citations_per_claim: int = 5
    max_evidence_quotes: int = 10
    max_claim_chars: int = 1_000
    max_evidence_quote_chars: int = 5_000
    max_question_chars: int = 2_000
    max_prompt_chars: int = 50_000

    def __post_init__(self) -> None:
        for name in (
            "max_context_chunks",
            "max_context_chars",
            "max_claims",
            "max_citations_per_claim",
            "max_evidence_quotes",
            "max_claim_chars",
            "max_evidence_quote_chars",
            "max_question_chars",
            "max_prompt_chars",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    """A question plus already authorized source chunks."""

    question: str
    chunks: tuple[RetrievedChunk, ...] = field(default_factory=tuple)
    limits: GenerationLimits = field(default_factory=GenerationLimits)

    def __post_init__(self) -> None:
        object.__setattr__(self, "question", _text(self.question, "question"))
        chunks = tuple(self.chunks)
        if any(not isinstance(chunk, RetrievedChunk) for chunk in chunks):
            raise TypeError("chunks must contain RetrievedChunk values")
        chunk_ids = [chunk.citation.chunk_id for chunk in chunks]
        if len(chunk_ids) != len(set(chunk_ids)):
            raise ValueError("generation context must not contain duplicate chunk IDs")
        object.__setattr__(self, "chunks", chunks)
        if not isinstance(self.limits, GenerationLimits):
            raise TypeError("limits must be GenerationLimits")


@dataclass(frozen=True, slots=True)
class AnswerModelRequest:
    """Versioned, provider-neutral request sent to a structured answer model."""

    prompt: str
    prompt_version: str = PROMPT_VERSION
    output_schema_version: str = OUTPUT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "prompt", _text(self.prompt, "prompt"))
        object.__setattr__(
            self,
            "prompt_version",
            _text(self.prompt_version, "prompt_version"),
        )
        object.__setattr__(
            self,
            "output_schema_version",
            _text(self.output_schema_version, "output_schema_version"),
        )


class AnswerModel(Protocol):
    """Provider boundary; the returned object is always treated as untrusted."""

    def generate(self, request: AnswerModelRequest) -> object: ...


@dataclass(frozen=True, slots=True)
class AnswerCitation:
    """Server-owned citation label mapped to exact source provenance."""

    citation_id: str
    chunk_id: str
    chunk_checksum: str
    document_id: str
    canonical_uri: str
    source_name: str
    version_id: str
    version_checksum: str
    version_number: int
    ordinal: int
    char_start: int
    char_end: int
    page_number: int | None
    section: str | None
    document_title: str
    published_at: datetime

    def __post_init__(self) -> None:
        if not re.fullmatch(r"S[1-9][0-9]*", self.citation_id):
            raise ValueError("citation_id must use the server S<number> format")
        for name in (
            "chunk_id",
            "document_id",
            "canonical_uri",
            "source_name",
            "version_id",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        for name in ("chunk_checksum", "version_checksum"):
            checksum = _text(getattr(self, name), name).lower()
            if not _SHA256.fullmatch(checksum):
                raise ValueError(f"{name} must be a hexadecimal SHA-256 digest")
            object.__setattr__(self, name, checksum)
        if isinstance(self.version_number, bool) or self.version_number <= 0:
            raise ValueError("version_number must be positive")
        if isinstance(self.ordinal, bool) or self.ordinal < 0:
            raise ValueError("ordinal must not be negative")
        if self.char_start < 0 or self.char_end <= self.char_start:
            raise ValueError("citation character range is invalid")
        if self.page_number is not None and self.page_number <= 0:
            raise ValueError("page_number must be positive")
        if self.section is not None:
            object.__setattr__(self, "section", _text(self.section, "section"))
        object.__setattr__(
            self,
            "document_title",
            _text(self.document_title, "document_title"),
        )
        if not isinstance(self.published_at, datetime):
            raise TypeError("published_at must be a datetime")
        if (
            self.published_at.tzinfo is None
            or self.published_at.utcoffset() is None
        ):
            raise ValueError("published_at must be timezone-aware")

    @classmethod
    def from_retrieval(cls, citation_id: str, citation: Citation) -> AnswerCitation:
        """Copy only authorized retrieval provenance into an answer citation."""
        document_title = getattr(citation, "document_title", None)
        published_at = getattr(citation, "published_at", None)
        if document_title is None or published_at is None:
            raise ValueError(
                "retrieval citation lacks required document title or publication time"
            )
        return cls(
            citation_id=citation_id,
            chunk_id=citation.chunk_id,
            chunk_checksum=citation.chunk_checksum,
            document_id=citation.document_id,
            canonical_uri=citation.canonical_uri,
            source_name=citation.source_name,
            version_id=citation.version_id,
            version_checksum=citation.version_checksum,
            version_number=citation.version_number,
            ordinal=citation.ordinal,
            char_start=citation.char_start,
            char_end=citation.char_end,
            page_number=citation.page_number,
            section=citation.section,
            document_title=document_title,
            published_at=published_at,
        )


@dataclass(frozen=True, slots=True)
class Claim:
    """One rendered material claim and its server-validated citations."""

    text: str
    material: bool
    citation_ids: tuple[str, ...]
    inference: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", _text(self.text, "claim text"))
        if _MODEL_CITATION_MARKER.search(self.text):
            raise ValueError("claim text must not contain model-authored citations")
        if not isinstance(self.material, bool):
            raise TypeError("material must be boolean")
        if not isinstance(self.inference, bool):
            raise TypeError("inference must be boolean")
        citation_ids = tuple(self.citation_ids)
        if not citation_ids:
            raise ValueError("every claim requires at least one citation")
        if len(citation_ids) != len(set(citation_ids)):
            raise ValueError("claim citation IDs must be unique")
        if any(not re.fullmatch(r"S[1-9][0-9]*", value) for value in citation_ids):
            raise ValueError("claim citation IDs must use the S<number> format")
        object.__setattr__(self, "citation_ids", citation_ids)


@dataclass(frozen=True, slots=True)
class Conflict:
    """Indexes of mutually conflicting sourced claims in an AnswerResult."""

    topic: str
    claim_indexes: tuple[int, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "topic", _text(self.topic, "conflict topic"))
        indexes = tuple(self.claim_indexes)
        if len(indexes) < 2 or len(indexes) != len(set(indexes)):
            raise ValueError("a conflict requires at least two unique claim indexes")
        if any(isinstance(index, bool) or index < 0 for index in indexes):
            raise ValueError("conflict claim indexes must not be negative")
        object.__setattr__(self, "claim_indexes", indexes)


@dataclass(frozen=True, slots=True)
class AnswerResult:
    """Auditable answer whose prose is rendered by the server, not the model."""

    status: AnswerStatus
    answer: str
    claims: tuple[Claim, ...]
    citations: tuple[AnswerCitation, ...]
    conflicts: tuple[Conflict, ...] = field(default_factory=tuple)
    prompt_version: str = PROMPT_VERSION
    output_schema_version: str = OUTPUT_SCHEMA_VERSION
    failure_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, AnswerStatus):
            raise TypeError("status must be an AnswerStatus")
        object.__setattr__(self, "answer", _text(self.answer, "answer"))
        object.__setattr__(
            self,
            "prompt_version",
            _text(self.prompt_version, "prompt_version"),
        )
        object.__setattr__(
            self,
            "output_schema_version",
            _text(self.output_schema_version, "output_schema_version"),
        )
        claims = tuple(self.claims)
        citations = tuple(self.citations)
        conflicts = tuple(self.conflicts)
        object.__setattr__(self, "claims", claims)
        object.__setattr__(self, "citations", citations)
        object.__setattr__(self, "conflicts", conflicts)
        if self.failure_code is not None:
            object.__setattr__(
                self,
                "failure_code",
                _text(self.failure_code, "failure_code"),
            )

        if self.status is AnswerStatus.INSUFFICIENT_CONTEXT:
            if claims or citations or conflicts:
                raise ValueError("an insufficient-context result cannot contain evidence")
            if self.answer != REFUSAL_ANSWER:
                raise ValueError("insufficient-context answers must use the standard refusal")
            return

        if self.failure_code is not None:
            raise ValueError("only an insufficient-context result may have a failure code")
        if not claims:
            raise ValueError("answered and conflict results require claims")
        if any(not claim.material for claim in claims):
            raise ValueError("every returned claim must be material")
        if self.status is AnswerStatus.ANSWERED and conflicts:
            raise ValueError("an answered result cannot contain conflicts")
        if self.status is AnswerStatus.CONFLICT and not conflicts:
            raise ValueError("a conflict result requires conflict details")

        citation_ids = [citation.citation_id for citation in citations]
        if len(citation_ids) != len(set(citation_ids)):
            raise ValueError("answer citation IDs must be unique")
        referenced = {
            citation_id for claim in claims for citation_id in claim.citation_ids
        }
        if referenced != set(citation_ids):
            raise ValueError("answer citations must exactly match referenced claim citations")
        for claim in claims:
            if claim.text not in self.answer:
                raise ValueError("answer must contain every structured claim")
            if claim.inference and f"Inference: {claim.text}" not in self.answer:
                raise ValueError("inference claims must be explicitly labelled")
            for citation_id in claim.citation_ids:
                if f"[{citation_id}]" not in self.answer:
                    raise ValueError("answer must contain inline citations for every claim")
        inline_ids = {value[1:-1] for value in _INLINE_CITATION.findall(self.answer)}
        if inline_ids != referenced:
            raise ValueError("answer contains an unknown or missing inline citation")
        conflict_indexes: list[int] = []
        for conflict in conflicts:
            if any(index >= len(claims) for index in conflict.claim_indexes):
                raise ValueError("conflict references an unknown claim index")
            conflict_indexes.extend(conflict.claim_indexes)
            if any(claims[index].inference for index in conflict.claim_indexes):
                raise ValueError("conflict alternatives must be sourced claims")
        if self.status is AnswerStatus.CONFLICT and (
            len(conflict_indexes) != len(set(conflict_indexes))
            or set(conflict_indexes) != set(range(len(claims)))
        ):
            raise ValueError("conflict details must cover every claim exactly once")

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-ready record for APIs and evaluation runners."""
        payload = asdict(self)
        payload["status"] = self.status.value
        for citation in payload["citations"]:
            citation["published_at"] = citation["published_at"].isoformat()
        return payload

    @classmethod
    def refusal(cls, *, failure_code: str | None = None) -> AnswerResult:
        return cls(
            status=AnswerStatus.INSUFFICIENT_CONTEXT,
            answer=REFUSAL_ANSWER,
            claims=(),
            citations=(),
            failure_code=failure_code,
        )
