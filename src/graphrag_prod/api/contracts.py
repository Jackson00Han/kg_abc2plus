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
from graphrag_prod.domain.models import TypedLiteralValue
from graphrag_prod.retrieval.models import RetrievalLimits, VersionFilter


MAX_DOCUMENT_BYTES = 5 * 1024 * 1024
MAX_QUERY_CHARS = 2_000
MAX_GROUPS = 64
MAX_FILTER_IDS = 100
MAX_GRAPH_ENTITIES = 100
MAX_GRAPH_ASSERTIONS = 100
MAX_GRAPH_PATHS = 100
MAX_GRAPH_EVIDENCE_PER_ENTITY = 20
MAX_GRAPH_CHUNK_CHARS = 50_000
MAX_GRAPH_TOTAL_EVIDENCE_CHARS = 500_000

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


LiteralSourceText = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=False,
        min_length=1,
        max_length=4_096,
    ),
]
LiteralUnitText = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=False,
        min_length=1,
        max_length=64,
    ),
]
LiteralTemporalText = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=False,
        min_length=1,
        max_length=64,
    ),
]


class TypedLiteralSemanticsResponse(StrictAPIModel):
    """Strict public projection of server-normalized literal semantics."""

    datatype: Literal[
        "STRING",
        "INTEGER",
        "FLOAT",
        "DECIMAL",
        "BOOLEAN",
        "DATE",
        "DATETIME",
        "DURATION",
        "URI",
        "JSON",
    ]
    typed_value: str | int | float | bool
    raw_value: LiteralSourceText
    raw_unit: LiteralUnitText | None = None
    canonical_value: LiteralSourceText
    canonical_unit: LiteralUnitText | None = None
    valid_from: AwareDatetime | None = None
    valid_to: AwareDatetime | None = None
    observed_at: AwareDatetime | None = None
    raw_valid_from: LiteralTemporalText | None = None
    raw_valid_to: LiteralTemporalText | None = None
    raw_observed_at: LiteralTemporalText | None = None

    @model_validator(mode="after")
    def validate_domain_semantics(self) -> Self:
        try:
            TypedLiteralValue(
                datatype=self.datatype,
                typed_value=self.typed_value,
                raw_value=self.raw_value,
                raw_unit=self.raw_unit,
                canonical_value=self.canonical_value,
                canonical_unit=self.canonical_unit,
                valid_from=self.valid_from,
                valid_to=self.valid_to,
                observed_at=self.observed_at,
                raw_valid_from=self.raw_valid_from,
                raw_valid_to=self.raw_valid_to,
                raw_observed_at=self.raw_observed_at,
            )
        except (TypeError, ValueError) as error:
            raise ValueError("typed literal semantics are inconsistent") from error
        return self


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
    include_graph: bool = True
    graph_trust_policy: Literal[
        "PUBLISHED_SECONDARY_INCLUSIVE",
        "AUTHORITATIVE_ONLY",
    ] = "PUBLISHED_SECONDARY_INCLUSIVE"

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
    """Terminal result from the synchronous ingestion endpoint."""

    job: JobResponse
    snapshot_id: Identifier | None = None
    active_snapshot_id: Identifier | None = None

    @model_validator(mode="after")
    def validate_terminal_job(self) -> Self:
        if self.job.status not in {"SUCCEEDED", "NOOP"}:
            raise ValueError("a successful synchronous write must be terminal")
        if self.job.phase != "COMPLETE":
            raise ValueError("a successful synchronous write must be complete")
        return self


class DeleteResponse(StrictAPIModel):
    """Terminal result from the synchronous deletion endpoint."""

    job: JobResponse

    @model_validator(mode="after")
    def validate_terminal_job(self) -> Self:
        if self.job.status not in {"SUCCEEDED", "NOOP"}:
            raise ValueError("a successful synchronous write must be terminal")
        if self.job.phase != "COMPLETE":
            raise ValueError("a successful synchronous write must be complete")
        return self


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


GraphName = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=True,
        min_length=1,
        max_length=512,
    ),
]
GraphTypeName = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z][A-Za-z0-9_]*$",
    ),
]
GraphExactText = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=False,
        min_length=1,
        max_length=MAX_GRAPH_CHUNK_CHARS,
    ),
]


class GraphCitationResponse(CitationResponse):
    """Exact authorized Chunk carried by a governed graph record."""

    chunk_text: GraphExactText
    document_title: Annotated[
        str,
        StringConstraints(strict=True, min_length=1, max_length=512),
    ]

    @model_validator(mode="after")
    def validate_exact_chunk(self) -> Self:
        if self.char_end - self.char_start != len(self.chunk_text):
            raise ValueError("graph citation text must match its exact Chunk range")
        checksum = hashlib.sha256(self.chunk_text.encode("utf-8")).hexdigest()
        if checksum != self.chunk_checksum:
            raise ValueError("graph citation text must match its Chunk checksum")
        return self


class GraphProvenanceResponse(StrictAPIModel):
    publication_id: Identifier
    record_id: Identifier
    revision_id: Identifier
    ontology_version_id: Identifier
    origin: Literal[
        "EXPERT_IMPORT",
        "EXPERT_CREATED",
        "LLM_EXTRACTED",
        "RULE_DERIVED",
        "FIXTURE",
    ]
    authority: Literal["AUTHORITATIVE", "SECONDARY"]
    status: Literal["PUBLISHED"]
    confidence: Annotated[float, Field(strict=True, ge=0.0, le=1.0)]
    extractor_version: GraphName | None = None
    prompt_version: GraphName | None = None

    @model_validator(mode="after")
    def validate_origin_authority(self) -> Self:
        expert = self.origin in {"EXPERT_IMPORT", "EXPERT_CREATED"}
        if expert != (self.authority == "AUTHORITATIVE"):
            raise ValueError("graph origin and authority are inconsistent")
        return self


class GraphEvidenceResponse(StrictAPIModel):
    citation: GraphCitationResponse
    char_start: Annotated[int, Field(strict=True, ge=0)]
    char_end: Annotated[int, Field(strict=True, ge=1)]
    quoted_text: GraphExactText
    provenance: GraphProvenanceResponse

    @model_validator(mode="after")
    def validate_exact_evidence(self) -> Self:
        if not (
            self.citation.char_start
            <= self.char_start
            < self.char_end
            <= self.citation.char_end
        ):
            raise ValueError("graph evidence range is outside its cited Chunk")
        start = self.char_start - self.citation.char_start
        end = self.char_end - self.citation.char_start
        if self.citation.chunk_text[start:end] != self.quoted_text:
            raise ValueError("graph evidence quote must match its exact Chunk span")
        return self


class GraphEntityResponse(StrictAPIModel):
    entity_id: Identifier
    entity_type: GraphTypeName
    canonical_key: GraphName
    canonical_name: GraphName
    aliases: Annotated[tuple[GraphName, ...], Field(max_length=100)]
    evidence: Annotated[
        tuple[GraphEvidenceResponse, ...],
        Field(min_length=1, max_length=MAX_GRAPH_EVIDENCE_PER_ENTITY),
    ]

    @model_validator(mode="after")
    def validate_unique_evidence(self) -> Self:
        if len(self.aliases) != len(set(self.aliases)):
            raise ValueError("graph Entity aliases must be unique")
        revisions = tuple(item.provenance.revision_id for item in self.evidence)
        if len(revisions) != len(set(revisions)):
            raise ValueError("graph Entity evidence revisions must be unique")
        return self


class GraphRelationshipPropertyResponse(StrictAPIModel):
    property_value_id: Identifier
    name: GraphTypeName
    literal_semantics: TypedLiteralSemanticsResponse
    evidence: GraphEvidenceResponse
    confidence: Annotated[float, Field(strict=True, ge=0.0, le=1.0)]


class GraphAssertionResponse(StrictAPIModel):
    record_id: Identifier
    revision_id: Identifier
    predicate: GraphTypeName
    subject_entity_id: Identifier
    subject_mention_revision_id: Identifier
    object_kind: Literal["entity", "literal"]
    object_entity_id: Identifier | None = None
    object_mention_revision_id: Identifier | None = None
    literal_value: GraphExactText | None = None
    literal_semantics: TypedLiteralSemanticsResponse | None = None
    relationship_properties: Annotated[
        tuple[GraphRelationshipPropertyResponse, ...],
        Field(max_length=MAX_GRAPH_ASSERTIONS),
    ] = ()
    evidence: GraphEvidenceResponse

    @field_validator("relationship_properties", mode="before")
    @classmethod
    def accept_json_relationship_properties(cls, value: object) -> object:
        return _json_array(value)

    @model_validator(mode="after")
    def validate_object_and_identity(self) -> Self:
        if (
            self.record_id != self.evidence.provenance.record_id
            or self.revision_id != self.evidence.provenance.revision_id
        ):
            raise ValueError("graph assertion identity must match its provenance")
        if self.object_kind == "entity":
            if (
                self.object_entity_id is None
                or self.object_mention_revision_id is None
                or self.literal_value is not None
                or self.literal_semantics is not None
            ):
                raise ValueError("relationship assertion object is invalid")
        elif (
            self.object_entity_id is not None
            or self.object_mention_revision_id is not None
            or self.literal_value is None
        ):
            raise ValueError("literal assertion object is invalid")
        if self.object_kind == "literal" and self.relationship_properties:
            raise ValueError("literal assertion cannot carry relationship properties")
        if (
            self.literal_semantics is not None
            and self.literal_semantics.raw_value != self.literal_value
        ):
            raise ValueError("literal semantics must match the raw literal value")
        return self


class GraphPathResponse(StrictAPIModel):
    subject_entity_id: Identifier
    assertion_revision_id: Identifier
    predicate: GraphTypeName
    object_entity_id: Identifier | None = None
    literal_value: GraphExactText | None = None
    literal_semantics: TypedLiteralSemanticsResponse | None = None
    relationship_properties: Annotated[
        tuple[GraphRelationshipPropertyResponse, ...],
        Field(max_length=MAX_GRAPH_ASSERTIONS),
    ] = ()
    evidence: GraphEvidenceResponse

    @field_validator("relationship_properties", mode="before")
    @classmethod
    def accept_json_relationship_properties(cls, value: object) -> object:
        return _json_array(value)

    @model_validator(mode="after")
    def validate_one_hop_object(self) -> Self:
        if (self.object_entity_id is None) == (self.literal_value is None):
            raise ValueError("graph path requires exactly one object")
        if self.object_entity_id is not None and self.literal_semantics is not None:
            raise ValueError("relationship path must not carry literal semantics")
        if self.object_entity_id is None and self.relationship_properties:
            raise ValueError("literal path cannot carry relationship properties")
        if (
            self.literal_semantics is not None
            and self.literal_semantics.raw_value != self.literal_value
        ):
            raise ValueError("path literal semantics must match its raw value")
        if self.assertion_revision_id != self.evidence.provenance.revision_id:
            raise ValueError("graph path must match its assertion evidence")
        return self


class EvidenceSubgraphResponse(StrictAPIModel):
    trust_policy: Literal[
        "PUBLISHED_SECONDARY_INCLUSIVE",
        "AUTHORITATIVE_ONLY",
    ]
    entities: Annotated[
        tuple[GraphEntityResponse, ...], Field(max_length=MAX_GRAPH_ENTITIES)
    ]
    relationship_assertions: Annotated[
        tuple[GraphAssertionResponse, ...], Field(max_length=MAX_GRAPH_ASSERTIONS)
    ]
    literal_assertions: Annotated[
        tuple[GraphAssertionResponse, ...], Field(max_length=MAX_GRAPH_ASSERTIONS)
    ]
    paths: Annotated[
        tuple[GraphPathResponse, ...], Field(max_length=MAX_GRAPH_PATHS)
    ]
    matched_chunk_ids: Annotated[
        tuple[Identifier, ...], Field(max_length=MAX_GRAPH_ASSERTIONS * 2)
    ]
    publication_ids: Annotated[tuple[Identifier, ...], Field(max_length=1)]

    @model_validator(mode="after")
    def validate_bounded_graph(self) -> Self:
        entity_ids = tuple(item.entity_id for item in self.entities)
        assertion_items = (*self.relationship_assertions, *self.literal_assertions)
        assertion_ids = tuple(item.revision_id for item in assertion_items)
        for name, values in (
            ("entities", entity_ids),
            ("assertions", assertion_ids),
            ("paths", tuple(item.assertion_revision_id for item in self.paths)),
            ("matched_chunk_ids", self.matched_chunk_ids),
            ("publication_ids", self.publication_ids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"graph {name} must be unique")
        known_entities = set(entity_ids)
        relationship_ids = {
            item.revision_id for item in self.relationship_assertions
        }
        literal_ids = {item.revision_id for item in self.literal_assertions}
        if any(
            item.object_kind != "entity"
            or item.subject_entity_id not in known_entities
            or item.object_entity_id not in known_entities
            for item in self.relationship_assertions
        ):
            raise ValueError("relationship assertions must reference graph Entities")
        if any(
            item.object_kind != "literal"
            or item.subject_entity_id not in known_entities
            for item in self.literal_assertions
        ):
            raise ValueError("literal assertions must reference graph Entities")
        by_revision = {item.revision_id: item for item in assertion_items}
        for path in self.paths:
            assertion = by_revision.get(path.assertion_revision_id)
            if (
                assertion is None
                or path.subject_entity_id != assertion.subject_entity_id
                or path.predicate != assertion.predicate
                or path.object_entity_id != assertion.object_entity_id
                or path.literal_value != assertion.literal_value
                or path.literal_semantics != assertion.literal_semantics
                or path.evidence != assertion.evidence
            ):
                raise ValueError("graph path must match one returned assertion")
        if relationship_ids.intersection(literal_ids):
            raise ValueError("graph assertion kinds must be disjoint")
        evidences = [
            evidence
            for entity in self.entities
            for evidence in entity.evidence
        ] + [item.evidence for item in assertion_items]
        citations: dict[str, GraphCitationResponse] = {}
        unique_evidence: dict[tuple[str, str], GraphEvidenceResponse] = {}
        for evidence in evidences:
            chunk_id = evidence.citation.chunk_id
            previous = citations.setdefault(chunk_id, evidence.citation)
            if previous != evidence.citation:
                raise ValueError("graph citations conflict for one Chunk")
            unique_evidence.setdefault(
                (evidence.provenance.revision_id, chunk_id), evidence
            )
        if set(self.matched_chunk_ids) != set(citations):
            raise ValueError("matched_chunk_ids must identify graph evidence Chunks")
        publications = {
            evidence.provenance.publication_id
            for evidence in unique_evidence.values()
        }
        if set(self.publication_ids) != publications:
            raise ValueError("publication_ids must identify graph provenance")
        if self.trust_policy == "AUTHORITATIVE_ONLY" and any(
            evidence.provenance.authority != "AUTHORITATIVE"
            for evidence in unique_evidence.values()
        ):
            raise ValueError("authoritative graph policy excluded secondary evidence")
        total_chars = sum(len(item.chunk_text) for item in citations.values()) + sum(
            len(item.quoted_text) for item in unique_evidence.values()
        )
        if total_chars > MAX_GRAPH_TOTAL_EVIDENCE_CHARS:
            raise ValueError("graph evidence exceeds the response character budget")
        return self


class RetrievalResponse(StrictAPIModel):
    chunks: Annotated[tuple[RetrievedChunkResponse, ...], Field(max_length=20)]
    trace: RetrievalTraceResponse
    graph: EvidenceSubgraphResponse | None = None

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
    "EvidenceSubgraphResponse",
    "GenerationLimitsRequest",
    "GraphAssertionResponse",
    "GraphCitationResponse",
    "GraphEntityResponse",
    "GraphEvidenceResponse",
    "GraphPathResponse",
    "GraphProvenanceResponse",
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
    "TypedLiteralSemanticsResponse",
    "VersionFilterRequest",
]
