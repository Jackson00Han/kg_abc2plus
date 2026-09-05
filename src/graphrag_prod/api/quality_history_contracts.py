"""Bounded, metadata-only contracts for immutable quality audit history."""

from typing import Annotated, Self

from pydantic import AwareDatetime, Field, field_validator, model_validator

from .contracts import Identifier, ShortText, StrictAPIModel, _json_array, _json_aware_datetime, _unique
from .knowledge_contracts import Digest, PublishedGraphQualityCountsResponse, PublishedGraphQualityResponse


class PublishedGraphQualityRecordRequest(StrictAPIModel):
    """An explicit audit-and-record action; all audit inputs are server-owned."""


class PublishedGraphQualityRunListRequest(StrictAPIModel):
    publication_id: Identifier | None = None
    limit: Annotated[int, Field(strict=True, ge=1, le=50)] = 10


class PublishedGraphQualityRunRequest(StrictAPIModel):
    run_id: Identifier


class PublishedGraphQualityRunResponse(StrictAPIModel):
    report: PublishedGraphQualityResponse
    recorded_by: Identifier
    recorded_at: AwareDatetime
    record_hash: Digest

    @field_validator("recorded_at", mode="before")
    @classmethod
    def accept_json_datetime(cls, value: object) -> object:
        return _json_aware_datetime(value)


class PublishedGraphQualityRunSummaryResponse(StrictAPIModel):
    run_id: Identifier
    publication_id: Identifier
    publication_generation: Annotated[int, Field(strict=True, ge=1, le=2_147_483_647)]
    ontology_version_id: Identifier
    corpus_revision: Annotated[int, Field(strict=True, ge=0, le=2_147_483_647)]
    graph_digest: Digest
    ruleset_version: ShortText
    passed: bool
    total_issue_count: Annotated[int, Field(strict=True, ge=0, le=2_147_483_647)]
    total_error_count: Annotated[int, Field(strict=True, ge=0, le=2_147_483_647)]
    issues_truncated: bool
    counts: PublishedGraphQualityCountsResponse
    recorded_by: Identifier
    recorded_at: AwareDatetime
    record_hash: Digest

    @field_validator("recorded_at", mode="before")
    @classmethod
    def accept_json_datetime(cls, value: object) -> object:
        return _json_aware_datetime(value)

    @model_validator(mode="after")
    def consistent_totals(self) -> Self:
        if self.total_error_count > self.total_issue_count:
            raise ValueError("quality error count cannot exceed issue count")
        if self.passed != (self.total_error_count == 0):
            raise ValueError("passed must match the quality error count")
        return self


class PublishedGraphQualityRunListResponse(StrictAPIModel):
    items: Annotated[tuple[PublishedGraphQualityRunSummaryResponse, ...], Field(max_length=50)]

    @field_validator("items", mode="before")
    @classmethod
    def accept_json_array(cls, value: object) -> object:
        return _json_array(value)

    @model_validator(mode="after")
    def unique_runs(self) -> Self:
        _unique(tuple(item.run_id for item in self.items), "quality run IDs")
        return self
