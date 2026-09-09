"""Version-bound human quality dispositions with bounded notes and server identity."""
from typing import Annotated, Literal
from pydantic import AwareDatetime, Field, StringConstraints, field_validator
from .contracts import StrictAPIModel, Identifier, _json_aware_datetime

class QualityReviewRequest(StrictAPIModel):
    run_id: Identifier
    issue_id: Identifier
    operation_key: Identifier
    decision: Literal['PENDING','NEEDS_CORRECTION','NO_CHANGE_REQUIRED']
    notes: Annotated[str,StringConstraints(strict=True,strip_whitespace=True,min_length=1,max_length=2000)]

class QualityReviewListRequest(StrictAPIModel):
    run_id: Identifier

class QualityReviewResponse(StrictAPIModel):
    review_id: Identifier
    run_id: Identifier
    issue_id: Identifier
    decision: Literal['PENDING','NEEDS_CORRECTION','NO_CHANGE_REQUIRED']
    notes: Annotated[str,StringConstraints(strict=True,min_length=1,max_length=2000)]
    recorded_by: Identifier
    recorded_at: AwareDatetime
    publication_id: Identifier
    publication_generation: Annotated[int,Field(strict=True,ge=1)]

    @field_validator('recorded_at',mode='before')
    @classmethod
    def date(cls,value): return _json_aware_datetime(value)

class QualityReviewListResponse(StrictAPIModel):
    items: Annotated[list[QualityReviewResponse],Field(max_length=100)]
    has_more: bool
    object_labels: Annotated[dict[Identifier,Annotated[str,StringConstraints(strict=True,max_length=1200)]],Field(max_length=5000)]
