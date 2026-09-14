"""Bounded public receipts for automatic review; never expose raw model prompts."""
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, field_validator

from .contracts import Identifier, StrictAPIModel, _json_aware_datetime

Count = Annotated[int, Field(strict=True, ge=0, le=1_000_000)]


class AutoReviewCounts(StrictAPIModel):
    entity_groups: Count = 0
    approved_groups: Count = 0
    approved_mentions: Count = 0
    approved_assertions: Count = 0
    manual_groups: Count = 0
    manual_assertions: Count = 0
    blocked_assertions: Count = 0
    incomplete: Count = 0


class AutoReviewItem(StrictAPIModel):
    record_id: Identifier
    input_revision: Annotated[int, Field(strict=True, ge=1)] | None = None
    record_kind: Literal["ENTITY_MENTION", "ASSERTION"]
    decision: Literal["AUTO_APPROVED", "NEEDS_HUMAN", "BLOCKED", "INCOMPLETE"]
    reason_code: Identifier
    reason: Annotated[str, Field(strict=True, max_length=4000)]
    target_entity_id: Identifier | None = None
    evidence_ids: Annotated[list[Identifier], Field(max_length=100)] = Field(default_factory=list)


class AutoReviewIssue(StrictAPIModel):
    code: Annotated[str, Field(strict=True, pattern=r"^(PROPERTY_REQUIRED|CONTEXT_[A-Z_]+)$", max_length=128)]
    property_name: Identifier
    entity_ids: Annotated[list[Identifier], Field(max_length=200)] = Field(default_factory=list)
    record_ids: Annotated[list[Identifier], Field(max_length=200)] = Field(default_factory=list)
    entity_count: Count
    reason: Annotated[str, Field(strict=True, max_length=4000)]
    source_paths: Annotated[list[Annotated[str, Field(strict=True, max_length=4096)]], Field(max_length=16)] = Field(default_factory=list)


class ContextMappingRuleResponse(StrictAPIModel):
    source_path: Annotated[str, Field(strict=True, max_length=4096)]
    target_collection: Annotated[str, Field(strict=True, max_length=4096)]
    property_name: Identifier
    scope_path: Annotated[str, Field(strict=True, max_length=4096)]
    binding_mode: Literal["ANCESTOR_DEFAULT", "IDENTITY_TEMPLATE"]
    status: Literal["COMPLETED", "PARTIAL"]
    applied: Count
    uncertain: Count
    overridden: Count
    reason: Annotated[str, Field(strict=True, max_length=400)]


class ContextMappingResponse(StrictAPIModel):
    status: Literal["RUNNING", "COMPLETED", "PARTIAL", "UNAVAILABLE", "SKIPPED"]
    added_assertions: Count
    applied: Count
    uncertain: Count
    overridden: Count
    rules: Annotated[list[ContextMappingRuleResponse], Field(max_length=64)] = Field(default_factory=list)
    issues: Annotated[list[AutoReviewIssue], Field(max_length=100)] = Field(default_factory=list)


class AutoReviewResponse(StrictAPIModel):
    job_id: Identifier
    run_id: Identifier
    status: Literal["RUNNING", "COMPLETED", "PARTIAL", "FAILED", "SKIPPED"]
    stage: Literal["CONTEXT", "IDENTITY", "FACTS", "DONE"]
    policy_version: Identifier
    initiated_by: Identifier
    reviewed_by: Identifier
    counts: AutoReviewCounts
    items: Annotated[list[AutoReviewItem], Field(max_length=200)] = Field(default_factory=list)
    issues: Annotated[list[AutoReviewIssue], Field(max_length=100)] = Field(default_factory=list)
    context_mapping: ContextMappingResponse | None = None
    truncated: bool = False
    model_calls: Count = 0
    updated_at: AwareDatetime

    @field_validator("updated_at", mode="before")
    @classmethod
    def persisted_timestamp(cls, value):
        return _json_aware_datetime(value)


class AutoReviewRunRequest(StrictAPIModel):
    retry: bool = False
    resume_identity_record_ids: Annotated[list[Identifier], Field(max_length=60)] = Field(default_factory=list)
