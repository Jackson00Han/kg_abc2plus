"""Business comparison is a read; rollback remains the existing guarded operation."""
from typing import Annotated, Literal
from pydantic import Field, StringConstraints
from .contracts import StrictAPIModel, Identifier, Checksum
from .knowledge_contracts import ShortText

class PublicationComparisonRequest(StrictAPIModel):
    target_publication_id: Identifier
    expected_active_publication_id: Identifier

class PublicationFactSummary(StrictAPIModel):
    revision_id: Identifier
    record_id: Identifier
    record_kind: Literal['ENTITY_MENTION','ASSERTION']
    subject_name: Annotated[str,StringConstraints(strict=True,max_length=512)]
    predicate: ShortText | None
    object_name: ShortText | None
    literal_value: Annotated[str,StringConstraints(strict=True,strip_whitespace=False,max_length=50000)] | None
    unit: ShortText | None
    valid_from: ShortText | None
    valid_to: ShortText | None
    observed_at: ShortText | None
    document_title: ShortText
    qualifier_digest: Checksum

class PublicationFactChange(StrictAPIModel):
    before: PublicationFactSummary
    after: PublicationFactSummary

class PublicationComparisonResponse(StrictAPIModel):
    expected_active_publication_id: Identifier
    target_publication_id: Identifier
    target_generation: Annotated[int,Field(strict=True,ge=1)]
    added: Annotated[list[PublicationFactSummary],Field(max_length=500)]
    removed: Annotated[list[PublicationFactSummary],Field(max_length=500)]
    changed: Annotated[list[PublicationFactChange],Field(max_length=500)]
    unchanged_count: Annotated[int,Field(strict=True,ge=0,le=500)]
