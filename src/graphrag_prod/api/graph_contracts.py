"""Strict public shapes for authorized graph browsing and exact evidence."""

from __future__ import annotations

from datetime import datetime
import json
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, StringConstraints, field_validator, model_validator

from graphrag_prod.graph.browse_models import GraphBrowseQuery, GraphReadPin
from graphrag_prod.graph.view_tokens import MAX_GRAPH_TOKEN_CHARS

from .contracts import (
    Checksum, GraphEvidenceResponse, GraphName, GraphTypeName, Identifier,
    IndustrialScopeRequest, StrictAPIModel, TypedLiteralSemanticsResponse, VersionFilterRequest,
)
from .knowledge_contracts import OntologyEntityType, OntologyHierarchy, OntologyRelationshipType


GraphToken = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=MAX_GRAPH_TOKEN_CHARS)]
GraphAuthority = Literal["AUTHORITATIVE", "SECONDARY"]
GraphOrigin = Literal["EXPERT_IMPORT", "EXPERT_CREATED", "LLM_EXTRACTED", "AUTHORITATIVE_EXTRACTED", "HUMAN_SUPPLEMENT", "RULE_DERIVED", "FIXTURE"]
SourceKind = Literal["CURATED_REFERENCE", "OFFICIAL_PUBLICATION", "SYNTHETIC_FIELD_RECORD", "USER_UPLOAD"]


def _array(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


class GraphBrowseRequest(StrictAPIModel):
    industrial_scope: IndustrialScopeRequest | None = None
    version_filter: VersionFilterRequest = Field(default_factory=VersionFilterRequest)
    trust_policy: Literal["PUBLISHED_SECONDARY_INCLUSIVE", "AUTHORITATIVE_ONLY"] = "PUBLISHED_SECONDARY_INCLUSIVE"
    entity_types: Annotated[tuple[GraphTypeName, ...], Field(max_length=64)] = ()
    predicates: Annotated[tuple[GraphTypeName, ...], Field(max_length=64)] = ()
    name_query: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)] | None = None
    seed_entity_ids: Annotated[tuple[Identifier, ...], Field(max_length=8)] = ()
    direction: Literal["both", "outgoing", "incoming"] = "both"
    hops: Annotated[int, Field(strict=True, ge=1, le=2)] = 1
    page_size: Annotated[int, Field(strict=True, ge=1, le=200)] = 100
    view_token: GraphToken | None = None
    cursor: GraphToken | None = None

    @field_validator("entity_types", "predicates", "seed_entity_ids", mode="before")
    @classmethod
    def arrays(cls, value: object) -> object:
        return _array(value)

    @model_validator(mode="after")
    def checked_query(self) -> Self:
        self.to_domain()
        if self.cursor is not None and self.view_token is None:
            raise ValueError("a graph cursor requires its view_token")
        return self

    def to_domain(self) -> GraphBrowseQuery:
        return GraphBrowseQuery(**self.model_dump(include={
            "trust_policy", "entity_types", "predicates", "name_query", "seed_entity_ids", "direction", "hops", "page_size",
        }))


class GraphEvidenceRequest(StrictAPIModel):
    view_token: GraphToken
    revision_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=10)]

    @field_validator("revision_ids", mode="before")
    @classmethod
    def arrays(cls, value: object) -> object:
        return _array(value)

    @model_validator(mode="after")
    def unique(self) -> Self:
        if len(set(self.revision_ids)) != len(self.revision_ids):
            raise ValueError("graph evidence revision IDs must be unique")
        return self


class GraphReadPinResponse(StrictAPIModel):
    publication_id: Identifier | None
    publication_generation: Annotated[int, Field(strict=True, ge=0)]
    activation_generation: Annotated[int, Field(strict=True, ge=0)]
    ontology_version_id: Identifier | None
    tbox_checksum: Checksum | None
    corpus_revision: Annotated[int, Field(strict=True, ge=0)]

    @model_validator(mode="after")
    def valid_pin(self) -> Self:
        GraphReadPin(**self.model_dump())
        return self


class GraphBrowseSchemaResponse(StrictAPIModel):
    entity_types: Annotated[tuple[OntologyEntityType, ...], Field(max_length=64)] = ()
    relationship_types: Annotated[tuple[OntologyRelationshipType, ...], Field(max_length=128)] = ()
    hierarchies: Annotated[tuple[OntologyHierarchy, ...], Field(max_length=16)] = ()


class GraphBrowseNodeResponse(StrictAPIModel):
    entity_id: Identifier
    entity_type: GraphTypeName
    label: GraphName
    canonical_key: GraphName
    authority_levels: Annotated[tuple[GraphAuthority, ...], Field(min_length=1, max_length=2)]
    mention_revision_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=500)]


class GraphBrowseEdgeResponse(StrictAPIModel):
    revision_id: Identifier
    record_id: Identifier
    source: Identifier
    target: Identifier
    predicate: GraphTypeName
    authority_level: GraphAuthority
    origin: GraphOrigin
    confidence: Annotated[float, Field(strict=True, ge=0, le=1)]
    source_kind: SourceKind | None = None


class GraphBrowseLiteralResponse(StrictAPIModel):
    revision_id: Identifier
    record_id: Identifier
    subject: Identifier
    predicate: GraphTypeName
    value: Annotated[str, StringConstraints(strict=True, strip_whitespace=False, min_length=1, max_length=50_000)]
    semantics: TypedLiteralSemanticsResponse | None = None
    authority_level: GraphAuthority
    origin: GraphOrigin
    confidence: Annotated[float, Field(strict=True, ge=0, le=1)]
    source_kind: SourceKind | None = None


class GraphBrowsePageResponse(StrictAPIModel):
    returned_nodes: Annotated[int, Field(strict=True, ge=0, le=150)]
    returned_edges: Annotated[int, Field(strict=True, ge=0, le=200)]
    returned_literals: Annotated[int, Field(strict=True, ge=0, le=200)]
    has_more: bool
    next_cursor: GraphToken | None


class GraphBrowseResponse(StrictAPIModel):
    view_token: GraphToken
    pin: GraphReadPinResponse
    graph_schema: GraphBrowseSchemaResponse = Field(alias="schema")
    nodes: Annotated[tuple[GraphBrowseNodeResponse, ...], Field(max_length=150)]
    edges: Annotated[tuple[GraphBrowseEdgeResponse, ...], Field(max_length=200)]
    literals: Annotated[tuple[GraphBrowseLiteralResponse, ...], Field(max_length=200)]
    page: GraphBrowsePageResponse
    visibility: Literal["AUTHORIZED_SOURCE_VIEW"] = "AUTHORIZED_SOURCE_VIEW"

    @model_validator(mode="after")
    def consistent_page(self) -> Self:
        ids = {item.entity_id for item in self.nodes}
        revisions = [item.revision_id for item in (*self.edges, *self.literals)]
        if len(ids) != len(self.nodes) or len(revisions) != len(set(revisions)):
            raise ValueError("graph response identities must be unique")
        if any(item.source not in ids or item.target not in ids for item in self.edges) or any(item.subject not in ids for item in self.literals):
            raise ValueError("graph assertions require visible endpoints")
        if (len(self.nodes), len(self.edges), len(self.literals)) != (self.page.returned_nodes, self.page.returned_edges, self.page.returned_literals) or self.page.has_more != (self.page.next_cursor is not None):
            raise ValueError("graph pagination metadata is inconsistent")
        return self


class GraphSourceApplicabilityResponse(StrictAPIModel):
    family: GraphName | None = None
    asset_keys: Annotated[tuple[Identifier, ...], Field(max_length=8)] = ()
    is_synthetic: bool | None = None
    project_curated_not_company_approved: bool | None = None
    sme_review_state: Literal["NOT_RECORDED"] = "NOT_RECORDED"


class GraphEvidenceItemResponse(StrictAPIModel):
    revision_id: Identifier
    record_id: Identifier
    record_kind: Literal["ENTITY_MENTION", "ASSERTION"]
    authority_level: GraphAuthority
    origin: GraphOrigin
    status: Literal["PUBLISHED"]
    confidence: Annotated[float, Field(strict=True, ge=0, le=1)]
    reviewed_by: GraphName | None
    reviewed_at: datetime | None
    review_notes: Annotated[str, StringConstraints(strict=True, max_length=4000)] | None
    source_kind: SourceKind | None
    applicability: GraphSourceApplicabilityResponse
    evidence: GraphEvidenceResponse


class GraphEvidenceResponseEnvelope(StrictAPIModel):
    view_token: GraphToken
    pin: GraphReadPinResponse
    items: Annotated[tuple[GraphEvidenceItemResponse, ...], Field(max_length=10)]

    @model_validator(mode="after")
    def consistent_evidence(self) -> Self:
        ids = [item.revision_id for item in self.items]
        if len(ids) != len(set(ids)) or sum(len(item.evidence.citation.chunk_text) + len(item.evidence.quoted_text) for item in self.items) > 500_000:
            raise ValueError("graph evidence exceeds its bound")
        for item in self.items:
            provenance = item.evidence.provenance
            if item.revision_id != provenance.revision_id or item.record_id != provenance.record_id or provenance.publication_id != self.pin.publication_id or provenance.ontology_version_id != self.pin.ontology_version_id or item.authority_level != provenance.authority or item.origin != provenance.origin or item.confidence != provenance.confidence:
                raise ValueError("graph evidence provenance is inconsistent")
        return self


class IndustrialSourcesRequest(StrictAPIModel):
    family: Literal["canalis-kt", "evopact-hvx-up24"] | None = None
    asset_keys: Annotated[tuple[Identifier, ...], Field(max_length=8)] = ()
    limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50

    @field_validator("asset_keys", mode="before")
    @classmethod
    def arrays(cls, value: object) -> object:
        return _array(value)

    @model_validator(mode="after")
    def scope_valid(self) -> Self:
        IndustrialScopeRequest(family=self.family, asset_keys=self.asset_keys)
        return self


class IndustrialSourceResponse(StrictAPIModel):
    document_id: Identifier
    version_id: Identifier
    title: GraphName
    source_kind: SourceKind
    family: Literal["canalis-kt", "evopact-hvx-up24"]
    asset_keys: Annotated[tuple[Identifier, ...], Field(max_length=8)]
    published_at: datetime | None
    chunk_count: Annotated[int, Field(strict=True, ge=1, le=10000)]
    first_chunk_id: Identifier
    canonical_uri: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=2048)] | None = None


class IndustrialSourcesResponse(StrictAPIModel):
    sources: Annotated[tuple[IndustrialSourceResponse, ...], Field(max_length=100)]
    has_more: bool


class IndustrialSourceChunkRequest(StrictAPIModel):
    chunk_id: Identifier


class IndustrialSourceChunkResponse(StrictAPIModel):
    source: IndustrialSourceResponse
    chunk_id: Identifier
    text: Annotated[str, StringConstraints(strict=True, strip_whitespace=False, min_length=1, max_length=50_000)]
    checksum: Checksum
    ordinal: Annotated[int, Field(strict=True, ge=0)]
    char_start: Annotated[int, Field(strict=True, ge=0)]
    char_end: Annotated[int, Field(strict=True, ge=1)]
    page_number: Annotated[int, Field(strict=True, ge=1)] | None
    section: GraphName | None
    provenance: dict[str, JsonValue]
    previous_chunk_id: Identifier | None = None
    next_chunk_id: Identifier | None = None

    @model_validator(mode="after")
    def exact_chunk(self) -> Self:
        from graphrag_prod.domain.ids import content_checksum
        if self.char_end - self.char_start != len(self.text) or content_checksum(self.text) != self.checksum or len(json.dumps(self.provenance, ensure_ascii=False).encode("utf-8")) > 512 * 1024:
            raise ValueError("source Chunk integrity or size is invalid")
        return self


class IndustrialSourceChunkEnvelope(StrictAPIModel):
    chunk: IndustrialSourceChunkResponse | None
