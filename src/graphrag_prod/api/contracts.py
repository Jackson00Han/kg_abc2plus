"""Strict, bounded HTTP request and response contracts.

Authentication-owned values are intentionally absent from client request
models: the controller derives tenant, principal groups, query vectors, and
embedding-space identity from trusted server state.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import re
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from graphrag_prod.generation.models import GenerationLimits, REFUSAL_ANSWER
from graphrag_prod.domain.ids import canonicalize_uri
from graphrag_prod.retrieval.models import RetrievalLimits, VersionFilter


MAX_DOCUMENT_BYTES = 5 * 1024 * 1024
MAX_QUERY_CHARS = 2_000
MAX_GROUPS = 64
MAX_FILTER_IDS = 100

Identifier = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=True,
        min_length=1,
        max_length=256,
        pattern=r"^[^\x00-\x20\x7f]+$",
    ),
]
GroupName = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]*$",
    ),
]
ShortText = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=512),
]
TraceReason = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=True,
        min_length=1,
        max_length=2_048,
    ),
]
class StrictAPIModel(BaseModel):
    """Default-deny model configuration shared by every API payload."""

    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        frozen=True,
        validate_default=True,
        validate_assignment=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
        str_strip_whitespace=True,
    )


def _unique(values: tuple[str, ...], name: str) -> tuple[str, ...]:
    if len(values) != len(set(values)):
        raise ValueError(f"{name} must not contain duplicates")
    return values


def _without_nul(value: str, name: str) -> str:
    if "\x00" in value:
        raise ValueError(f"{name} must not contain NUL")
    return value


def _safe_metadata_text(value: str, name: str) -> str:
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError(f"{name} must not contain control characters")
    return value


def _canonical_source_uri(value: str) -> str:
    """Accept only a credential-free, stable source identity URI.

    Query strings are deliberately excluded: they frequently contain signed
    credentials or volatile tracking values and would otherwise be reflected
    in citations.  Fragments are not part of the authoritative document
    identity; exact locations belong in Chunk provenance instead.
    """
    try:
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("canonical_uri must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("canonical_uri must not contain a query or fragment")
        canonical = canonicalize_uri(value)
    except (TypeError, ValueError) as error:
        raise ValueError("canonical_uri is invalid") from error
    return canonical


def _json_array(value: object) -> object:
    """Preserve strict items while accepting the JSON array decoded by ASGI."""
    if isinstance(value, list):
        return tuple(value)
    return value


def _json_aware_datetime(value: object) -> object:
    """Parse only an ISO-8601 JSON string; never coerce numbers or booleans."""
    if not isinstance(value, str):
        return value
    if not value or len(value) > 64 or "\x00" in value:
        return value
    normalized = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return value


class IngestionRequest(StrictAPIModel):
    """One authoritative UTF-8 text version submitted for ingestion."""

    operation_key: Annotated[
        str,
        StringConstraints(
            strict=True,
            strip_whitespace=True,
            min_length=16,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        ),
    ]
    canonical_uri: Annotated[
        str,
        StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=2_048),
    ]
    title: ShortText
    source_name: Annotated[
        str,
        StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=256),
    ]
    mime_type: Literal["text/plain"] = "text/plain"
    language: Literal["en"] = "en"
    published_at: AwareDatetime | None = None
    # Source content is not stripped or normalized here: its exact bytes are
    # part of provenance and checksum calculation.
    content: Annotated[
        str,
        StringConstraints(
            strict=True,
            strip_whitespace=False,
            min_length=1,
            max_length=MAX_DOCUMENT_BYTES,
        ),
    ]
    access_policy_id: Identifier
    access_policy_version: Annotated[int, Field(strict=True, ge=1, le=2_147_483_647)]
    access_groups: Annotated[
        tuple[GroupName, ...],
        Field(min_length=1, max_length=MAX_GROUPS),
    ]
    expected_active_snapshot_id: Identifier | None = None
    source_generation: Annotated[int, Field(strict=True, ge=0, le=9_223_372_036_854_775_807)] = 0
    max_attempts: Annotated[int, Field(strict=True, ge=1, le=10)] = 3

    @field_validator("published_at", mode="before")
    @classmethod
    def validate_json_published_at(cls, value: object) -> object:
        return _json_aware_datetime(value)

    @field_validator("access_groups", mode="before")
    @classmethod
    def validate_json_access_groups(cls, value: object) -> object:
        return _json_array(value)

    @field_validator("canonical_uri")
    @classmethod
    def validate_canonical_uri(cls, value: str) -> str:
        return _canonical_source_uri(value)

    @field_validator("title", "source_name")
    @classmethod
    def validate_safe_text(cls, value: str, info: object) -> str:
        return _safe_metadata_text(value, getattr(info, "field_name", "text"))

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        _without_nul(value, "content")
        try:
            size = len(value.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise ValueError("content must be valid UTF-8 text") from error
        if size > MAX_DOCUMENT_BYTES:
            raise ValueError("content exceeds the 5 MiB UTF-8 limit")
        return value

    @field_validator("access_groups")
    @classmethod
    def validate_access_groups(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "access_groups")


class DeleteRequest(StrictAPIModel):
    operation_key: Annotated[
        str,
        StringConstraints(
            strict=True,
            strip_whitespace=True,
            min_length=16,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        ),
    ]
    expected_active_snapshot_id: Identifier | None = None
    source_generation: Annotated[int, Field(strict=True, ge=0, le=9_223_372_036_854_775_807)] = 0
    max_attempts: Annotated[int, Field(strict=True, ge=1, le=10)] = 3


class VersionFilterRequest(StrictAPIModel):
    document_ids: Annotated[tuple[Identifier, ...], Field(max_length=MAX_FILTER_IDS)] = ()
    version_ids: Annotated[tuple[Identifier, ...], Field(max_length=MAX_FILTER_IDS)] = ()
    published_at_or_before: AwareDatetime | None = None

    @field_validator("published_at_or_before", mode="before")
    @classmethod
    def validate_json_cutoff(cls, value: object) -> object:
        return _json_aware_datetime(value)

    @field_validator("document_ids", "version_ids", mode="before")
    @classmethod
    def validate_json_ids(cls, value: object) -> object:
        return _json_array(value)

    @field_validator("document_ids", "version_ids")
    @classmethod
    def validate_ids(cls, value: tuple[str, ...], info: object) -> tuple[str, ...]:
        return _unique(value, getattr(info, "field_name", "ids"))

    @model_validator(mode="after")
    def validate_total_ids(self) -> Self:
        if len(self.document_ids) + len(self.version_ids) > MAX_FILTER_IDS:
            raise ValueError("version filter cannot contain more than 100 total IDs")
        return self

    def to_domain(self) -> VersionFilter:
        return VersionFilter(
            document_ids=frozenset(self.document_ids),
            version_ids=frozenset(self.version_ids),
            published_at_or_before=self.published_at_or_before,
        )


class RetrievalLimitsRequest(StrictAPIModel):
    """Client-tunable limits, each capped by a server safety maximum."""

    top_k: Annotated[int, Field(strict=True, ge=1, le=20)] = 5
    vector_recall_k: Annotated[int, Field(strict=True, ge=1, le=100)] = 20
    bm25_recall_k: Annotated[int, Field(strict=True, ge=1, le=100)] = 20
    bm25_scan_k: Annotated[int, Field(strict=True, ge=1, le=500)] = 100
    seed_k: Annotated[int, Field(strict=True, ge=1, le=20)] = 5
    graph_entities_per_seed: Annotated[int, Field(strict=True, ge=1, le=50)] = 20
    graph_edges_per_seed: Annotated[int, Field(strict=True, ge=1, le=200)] = 100
    graph_candidates_per_seed: Annotated[int, Field(strict=True, ge=1, le=50)] = 20
    candidate_limit: Annotated[int, Field(strict=True, ge=1, le=200)] = 100
    anchor_k: Annotated[int, Field(strict=True, ge=1, le=20)] = 3
    adjacent_window: Annotated[int, Field(strict=True, ge=0, le=3)] = 1
    max_context_chars: Annotated[int, Field(strict=True, ge=256, le=30_000)] = 12_000
    rrf_rank_constant: Annotated[int, Field(strict=True, ge=1, le=1_000)] = 60
    minimum_vector_score: Annotated[float, Field(strict=True, ge=0.0, le=1.0)] = 0.0
    minimum_bm25_score: Annotated[float, Field(strict=True, ge=0.0, le=1_000_000.0)] = 0.0
    minimum_rrf_channels: Literal[1, 2] = 1
    deduplicate_content: bool = True

    @model_validator(mode="after")
    def validate_relational_limits(self) -> Self:
        if self.bm25_scan_k < self.bm25_recall_k:
            raise ValueError("bm25_scan_k must cover bm25_recall_k")
        if self.seed_k > self.candidate_limit:
            raise ValueError("seed_k must not exceed candidate_limit")
        if self.anchor_k > self.top_k:
            raise ValueError("anchor_k must not exceed top_k")
        return self

    def to_domain(self) -> RetrievalLimits:
        return RetrievalLimits(**self.model_dump())


class GenerationLimitsRequest(StrictAPIModel):
    max_context_chunks: Annotated[int, Field(strict=True, ge=1, le=10)] = 10
    max_context_chars: Annotated[int, Field(strict=True, ge=256, le=20_000)] = 20_000
    max_claims: Annotated[int, Field(strict=True, ge=1, le=20)] = 20
    max_citations_per_claim: Annotated[int, Field(strict=True, ge=1, le=5)] = 5
    max_evidence_quotes: Annotated[int, Field(strict=True, ge=1, le=10)] = 10
    max_claim_chars: Annotated[int, Field(strict=True, ge=32, le=1_000)] = 1_000
    max_evidence_quote_chars: Annotated[int, Field(strict=True, ge=32, le=5_000)] = 5_000
    max_question_chars: Annotated[int, Field(strict=True, ge=32, le=MAX_QUERY_CHARS)] = MAX_QUERY_CHARS
    max_prompt_chars: Annotated[int, Field(strict=True, ge=1_000, le=50_000)] = 50_000

    def to_domain(self) -> GenerationLimits:
        return GenerationLimits(**self.model_dump())


class RetrievalRequest(StrictAPIModel):
    query_text: Annotated[
        str,
        StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=MAX_QUERY_CHARS),
    ]
    version_filter: VersionFilterRequest = Field(default_factory=VersionFilterRequest)
    limits: RetrievalLimitsRequest = Field(default_factory=RetrievalLimitsRequest)

    @field_validator("query_text")
    @classmethod
    def validate_query(cls, value: str) -> str:
        return _without_nul(value, "query_text")


class AnswerRequest(StrictAPIModel):
    query_text: Annotated[
        str,
        StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=MAX_QUERY_CHARS),
    ]
    version_filter: VersionFilterRequest = Field(default_factory=VersionFilterRequest)
    retrieval_limits: RetrievalLimitsRequest = Field(default_factory=RetrievalLimitsRequest)
    generation_limits: GenerationLimitsRequest = Field(default_factory=GenerationLimitsRequest)

    @field_validator("query_text")
    @classmethod
    def validate_query(cls, value: str) -> str:
        return _without_nul(value, "query_text")


class JobResponse(StrictAPIModel):
    """Safe job projection; lease owner/token and request hashes stay internal."""

    job_id: Identifier
    operation: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=32)]
    status: Literal[
        "QUEUED",
        "RUNNING",
        "RETRY_WAIT",
        "SUCCEEDED",
        "NOOP",
        "FAILED_PERMANENT",
    ]
    phase: Literal["PLAN", "STAGE", "VERIFY", "PUBLISH", "CLEANUP", "COMPLETE"]
    document_id: Identifier
    target_version_id: Identifier | None = None
    target_snapshot_id: Identifier | None = None
    expected_active_snapshot_id: Identifier | None = None
    source_generation: Annotated[int, Field(strict=True, ge=0)]
    attempts: Annotated[int, Field(strict=True, ge=0, le=10)]
    max_attempts: Annotated[int, Field(strict=True, ge=1, le=10)]
    completed_tasks: Annotated[int, Field(strict=True, ge=0)]
    expected_tasks: Annotated[int, Field(strict=True, ge=0)]
    outcome: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)] | None = None
    last_error_code: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64)] | None = None


class IngestionResponse(StrictAPIModel):
    job: JobResponse
    snapshot_id: Identifier | None = None
    active_snapshot_id: Identifier | None = None


class DeleteResponse(StrictAPIModel):
    job: JobResponse


class CitationResponse(StrictAPIModel):
    chunk_id: Identifier
    chunk_checksum: Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
    document_id: Identifier
    canonical_uri: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=2_048)]
    source_name: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)]
    version_id: Identifier
    version_checksum: Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
    version_number: Annotated[int, Field(strict=True, ge=1)]
    ordinal: Annotated[int, Field(strict=True, ge=0)]
    char_start: Annotated[int, Field(strict=True, ge=0)]
    char_end: Annotated[int, Field(strict=True, ge=1)]
    page_number: Annotated[int, Field(strict=True, ge=1)] | None = None
    section: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=512)] | None = None
    document_title: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=512)] | None = None
    published_at: AwareDatetime | None = None

    @field_validator("canonical_uri")
    @classmethod
    def validate_canonical_uri(cls, value: str) -> str:
        return _canonical_source_uri(value)

    @field_validator("source_name", "section", "document_title")
    @classmethod
    def validate_safe_metadata(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return _safe_metadata_text(value, getattr(info, "field_name", "text"))

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if self.char_end <= self.char_start:
            raise ValueError("citation character range is invalid")
        return self


class RetrievedChunkResponse(StrictAPIModel):
    # Exact source whitespace is part of the Chunk checksum and character
    # range.  It must override the API model's safe metadata normalization.
    text: Annotated[
        str,
        StringConstraints(
            strict=True,
            strip_whitespace=False,
            min_length=1,
            max_length=30_000,
        ),
    ]
    citation: CitationResponse
    role: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=32)]
    score: Annotated[float, Field(strict=True)] | None
    reasons: Annotated[tuple[TraceReason, ...], Field(max_length=32)]

    @model_validator(mode="after")
    def validate_exact_chunk(self) -> Self:
        if self.citation.char_end - self.citation.char_start != len(self.text):
            raise ValueError("retrieved text must match its exact source range")
        checksum = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        if checksum != self.citation.chunk_checksum:
            raise ValueError("retrieved text must match its Chunk checksum")
        return self


class TraceHitResponse(StrictAPIModel):
    chunk_id: Identifier
    rank: Annotated[int, Field(strict=True, ge=1, le=10_000)]
    score: Annotated[float, Field(strict=True)] | None
    ranks: Annotated[tuple[tuple[ShortText, Annotated[int, Field(strict=True, ge=1)]], ...], Field(max_length=8)] = ()
    reasons: Annotated[tuple[TraceReason, ...], Field(max_length=32)] = ()


class TraceDecisionResponse(StrictAPIModel):
    chunk_id: Identifier
    decision: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64)]
    reason: ShortText


class RetrievalTraceResponse(StrictAPIModel):
    trace_id: Identifier
    method: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=512)]
    tenant_id: Identifier
    corpus_revision: Annotated[int, Field(strict=True, ge=0)]
    embedding_generation_id: Identifier
    embedding_space_id: Identifier
    vector_recall: Annotated[tuple[TraceHitResponse, ...], Field(max_length=100)]
    bm25_recall: Annotated[tuple[TraceHitResponse, ...], Field(max_length=100)]
    seed_ranking: Annotated[tuple[TraceHitResponse, ...], Field(max_length=200)]
    graph_expansion: Annotated[tuple[TraceHitResponse, ...], Field(max_length=1_000)]
    candidate_vector_ranking: Annotated[tuple[TraceHitResponse, ...], Field(max_length=200)]
    final_ranking: Annotated[tuple[TraceHitResponse, ...], Field(max_length=200)]
    decisions: Annotated[tuple[TraceDecisionResponse, ...], Field(max_length=2_000)]
    selected_chunk_ids: Annotated[tuple[Identifier, ...], Field(max_length=20)]
    context_chars: Annotated[int, Field(strict=True, ge=0, le=30_000)]
    limits: RetrievalLimitsRequest
    version_filter: VersionFilterRequest


class RetrievalResponse(StrictAPIModel):
    chunks: Annotated[tuple[RetrievedChunkResponse, ...], Field(max_length=20)]
    trace: RetrievalTraceResponse

    @model_validator(mode="after")
    def validate_trace_selection(self) -> Self:
        chunk_ids = tuple(chunk.citation.chunk_id for chunk in self.chunks)
        if len(chunk_ids) != len(set(chunk_ids)):
            raise ValueError("retrieval response cannot contain duplicate Chunks")
        if self.trace.selected_chunk_ids != chunk_ids:
            raise ValueError("retrieval trace must exactly identify returned Chunks")
        if self.trace.context_chars != sum(len(chunk.text) for chunk in self.chunks):
            raise ValueError("retrieval context character count is inconsistent")
        return self


class AnswerCitationResponse(CitationResponse):
    citation_id: Annotated[
        str,
        StringConstraints(strict=True, pattern=r"^S[1-9][0-9]*$", max_length=8),
    ]
    document_title: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=512)]
    published_at: AwareDatetime


class ClaimResponse(StrictAPIModel):
    text: Annotated[str, Field(strict=True, min_length=1, max_length=1_000)]
    material: Literal[True]
    citation_ids: Annotated[
        tuple[
            Annotated[
                str,
                StringConstraints(strict=True, pattern=r"^S[1-9][0-9]*$", max_length=8),
            ],
            ...,
        ],
        Field(min_length=1, max_length=5),
    ]
    inference: bool

    @field_validator("citation_ids")
    @classmethod
    def validate_citation_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "citation_ids")

    @field_validator("text")
    @classmethod
    def validate_server_owned_citations(cls, value: str) -> str:
        if re.search(r"\[\s*(?:S\s*\d+|source\b|citation\b)", value, re.I):
            raise ValueError("claim text cannot contain authored citation markers")
        return value


class ConflictResponse(StrictAPIModel):
    topic: Annotated[str, Field(strict=True, min_length=1, max_length=1_000)]
    claim_indexes: Annotated[
        tuple[Annotated[int, Field(strict=True, ge=0, le=19)], ...],
        Field(min_length=2, max_length=20),
    ]

    @field_validator("claim_indexes")
    @classmethod
    def validate_claim_indexes(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if len(value) != len(set(value)):
            raise ValueError("claim_indexes must not contain duplicates")
        return value


class AnswerResponse(StrictAPIModel):
    status: Literal["answered", "insufficient_context", "conflict"]
    answer: Annotated[str, Field(strict=True, min_length=1, max_length=30_000)]
    claims: Annotated[tuple[ClaimResponse, ...], Field(max_length=20)]
    citations: Annotated[tuple[AnswerCitationResponse, ...], Field(max_length=100)]
    conflicts: Annotated[tuple[ConflictResponse, ...], Field(max_length=10)] = ()
    prompt_version: Identifier
    output_schema_version: Identifier
    failure_code: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64)] | None = None

    @model_validator(mode="after")
    def validate_grounded_answer_shape(self) -> Self:
        if self.status == "insufficient_context":
            if self.claims or self.citations or self.conflicts:
                raise ValueError("insufficient context cannot return evidence")
            if self.answer != REFUSAL_ANSWER:
                raise ValueError("insufficient context must use the standard refusal")
            return self

        if self.failure_code is not None or not self.claims:
            raise ValueError("answered and conflict responses require supported claims")
        if self.status == "answered" and self.conflicts:
            raise ValueError("an answered response cannot contain conflicts")
        if self.status == "conflict" and not self.conflicts:
            raise ValueError("a conflict response requires conflict details")

        citation_ids = tuple(citation.citation_id for citation in self.citations)
        if len(citation_ids) != len(set(citation_ids)):
            raise ValueError("answer citation IDs must be unique")
        referenced = {
            citation_id
            for claim in self.claims
            for citation_id in claim.citation_ids
        }
        if referenced != set(citation_ids):
            raise ValueError("answer citations must exactly match claim references")
        for claim in self.claims:
            if claim.text not in self.answer:
                raise ValueError("answer must contain every structured claim")
            if claim.inference and f"Inference: {claim.text}" not in self.answer:
                raise ValueError("inferences must be explicitly labelled")
            if any(f"[{citation_id}]" not in self.answer for citation_id in claim.citation_ids):
                raise ValueError("answer must contain each inline claim citation")
        inline_ids = set(re.findall(r"\[(S[1-9][0-9]*)\]", self.answer))
        if inline_ids != referenced:
            raise ValueError("answer contains an unknown or missing inline citation")

        conflict_indexes = [
            index for conflict in self.conflicts for index in conflict.claim_indexes
        ]
        if any(index >= len(self.claims) for index in conflict_indexes):
            raise ValueError("conflict references an unknown claim")
        if self.status == "conflict" and (
            len(conflict_indexes) != len(set(conflict_indexes))
            or set(conflict_indexes) != set(range(len(self.claims)))
            or any(self.claims[index].inference for index in conflict_indexes)
        ):
            raise ValueError("conflicts must cover each sourced claim exactly once")
        return self


class HealthResponse(StrictAPIModel):
    status: Literal["ok"] = "ok"
    service: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)]
    version: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64)]


class ReadinessResponse(StrictAPIModel):
    status: Literal["ready", "not_ready"]
    checks: dict[
        Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64)],
        Literal["ok", "error"],
    ]

    @field_validator("checks")
    @classmethod
    def validate_checks(cls, value: dict[str, str]) -> dict[str, str]:
        if not 1 <= len(value) <= 16:
            raise ValueError("checks must contain between one and sixteen entries")
        return value


class DurationBucketsResponse(StrictAPIModel):
    le_1: Annotated[int, Field(strict=True, ge=0)]
    le_5: Annotated[int, Field(strict=True, ge=0)]
    le_10: Annotated[int, Field(strict=True, ge=0)]
    le_25: Annotated[int, Field(strict=True, ge=0)]
    le_50: Annotated[int, Field(strict=True, ge=0)]
    le_100: Annotated[int, Field(strict=True, ge=0)]
    le_250: Annotated[int, Field(strict=True, ge=0)]
    le_500: Annotated[int, Field(strict=True, ge=0)]
    le_1000: Annotated[int, Field(strict=True, ge=0)]
    le_5000: Annotated[int, Field(strict=True, ge=0)]
    gt_5000: Annotated[int, Field(strict=True, ge=0)]


class DurationAggregateResponse(StrictAPIModel):
    count: Annotated[int, Field(strict=True, ge=0)]
    total_ms: Annotated[float, Field(strict=True, ge=0.0)]
    min_ms: Annotated[float, Field(strict=True, ge=0.0)] | None
    max_ms: Annotated[float, Field(strict=True, ge=0.0)] | None
    buckets: DurationBucketsResponse

    @model_validator(mode="after")
    def validate_duration_aggregate(self) -> Self:
        if self.count == 0:
            if self.min_ms is not None or self.max_ms is not None:
                raise ValueError("an empty duration aggregate cannot have extrema")
        elif self.min_ms is None or self.max_ms is None:
            raise ValueError("a non-empty duration aggregate requires extrema")
        elif self.min_ms > self.max_ms:
            raise ValueError("duration minimum cannot exceed maximum")
        if self.buckets.le_5000 + self.buckets.gt_5000 != self.count:
            raise ValueError("duration terminal buckets must cover the aggregate count")
        return self


class RouteMetricsResponse(StrictAPIModel):
    count: Annotated[int, Field(strict=True, ge=0)]
    error_count: Annotated[int, Field(strict=True, ge=0)]
    latency_ms: DurationAggregateResponse

    @model_validator(mode="after")
    def validate_route_aggregate(self) -> Self:
        if self.error_count > self.count:
            raise ValueError("route error_count cannot exceed count")
        if self.latency_ms.count != self.count:
            raise ValueError("route latency count must equal request count")
        return self


MetricLabel = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=256),
]


class RequestsMetricsResponse(StrictAPIModel):
    total: Annotated[int, Field(strict=True, ge=0)]
    error_count: Annotated[int, Field(strict=True, ge=0)]
    by_route: dict[MetricLabel, RouteMetricsResponse]

    @model_validator(mode="after")
    def validate_request_totals(self) -> Self:
        if len(self.by_route) > 512:
            raise ValueError("by_route exceeds its cardinality bound")
        if sum(item.count for item in self.by_route.values()) != self.total:
            raise ValueError("by_route counts must equal request total")
        if sum(item.error_count for item in self.by_route.values()) != self.error_count:
            raise ValueError("by_route errors must equal request error_count")
        return self


class ErrorsMetricsResponse(StrictAPIModel):
    total: Annotated[int, Field(strict=True, ge=0)]
    by_code: dict[MetricLabel, Annotated[int, Field(strict=True, ge=0)]]

    @model_validator(mode="after")
    def validate_error_totals(self) -> Self:
        if len(self.by_code) > 64:
            raise ValueError("by_code exceeds its cardinality bound")
        if sum(self.by_code.values()) != self.total:
            raise ValueError("by_code counts must equal error total")
        return self


class RetrievalMetricsResponse(StrictAPIModel):
    total: DurationAggregateResponse
    by_stage: dict[MetricLabel, DurationAggregateResponse]

    @model_validator(mode="after")
    def validate_stage_totals(self) -> Self:
        if len(self.by_stage) > 32:
            raise ValueError("by_stage exceeds its cardinality bound")
        if sum(item.count for item in self.by_stage.values()) != self.total.count:
            raise ValueError("by_stage counts must equal retrieval total")
        return self


class ModelMetricsResponse(StrictAPIModel):
    calls: Annotated[int, Field(strict=True, ge=0)]
    input_tokens: Annotated[int, Field(strict=True, ge=0)]
    output_tokens: Annotated[int, Field(strict=True, ge=0)]
    estimated_cost_usd: Annotated[float, Field(strict=True, ge=0.0)]


class MetricsResponse(StrictAPIModel):
    """Exact typed projection of ``MetricsRegistry.snapshot()``."""

    requests: RequestsMetricsResponse
    errors: ErrorsMetricsResponse
    retrieval: RetrievalMetricsResponse
    model: ModelMetricsResponse
    generated_at: AwareDatetime | None = None
    in_flight: Annotated[int, Field(strict=True, ge=0)] = 0


class ErrorResponse(StrictAPIModel):
    code: Annotated[
        str,
        StringConstraints(
            strict=True,
            min_length=1,
            max_length=64,
            pattern=r"^[a-z][a-z0-9_]*$",
        ),
    ]
    message: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)]
    request_id: Identifier


# Readable aliases for route code and backwards-compatible naming during the
# Stage 7 integration work.
IngestDocumentRequest = IngestionRequest
DeleteDocumentRequest = DeleteRequest
JobStatusResponse = JobResponse


__all__ = [
    "AnswerCitationResponse",
    "AnswerRequest",
    "AnswerResponse",
    "CitationResponse",
    "ClaimResponse",
    "ConflictResponse",
    "DeleteDocumentRequest",
    "DeleteRequest",
    "DeleteResponse",
    "ErrorResponse",
    "ErrorsMetricsResponse",
    "GenerationLimitsRequest",
    "HealthResponse",
    "IngestDocumentRequest",
    "IngestionRequest",
    "IngestionResponse",
    "JobResponse",
    "JobStatusResponse",
    "MAX_DOCUMENT_BYTES",
    "MAX_FILTER_IDS",
    "MAX_GROUPS",
    "MAX_QUERY_CHARS",
    "MetricsResponse",
    "ModelMetricsResponse",
    "ReadinessResponse",
    "RequestsMetricsResponse",
    "RetrievalLimitsRequest",
    "RetrievalMetricsResponse",
    "RetrievalRequest",
    "RetrievalResponse",
    "RetrievalTraceResponse",
    "RetrievedChunkResponse",
    "StrictAPIModel",
    "DurationAggregateResponse",
    "DurationBucketsResponse",
    "RouteMetricsResponse",
    "TraceDecisionResponse",
    "TraceHitResponse",
    "VersionFilterRequest",
]
