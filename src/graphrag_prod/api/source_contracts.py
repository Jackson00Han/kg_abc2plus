"""Read-only source library contracts; identity always comes from authentication."""
from hashlib import sha256
from typing import Annotated
from pydantic import Field, StringConstraints, model_validator
from .contracts import StrictAPIModel, Identifier, Checksum
from .knowledge_contracts import ShortText, DocumentCanonicalUri

class SourceListRequest(StrictAPIModel):
    after: Identifier | None = None
    limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 30

class SourceReadRequest(StrictAPIModel):
    document_id: Identifier
    version_id: Identifier
    ordinal: Annotated[int, Field(strict=True, ge=0, le=2_147_483_647)] = 0

class SourceSummary(StrictAPIModel):
    document_id: Identifier
    version_id: Identifier
    version_number: Annotated[int, Field(strict=True, ge=1)]
    title: ShortText
    source_name: ShortText
    canonical_uri: DocumentCanonicalUri
    chunk_count: Annotated[int, Field(strict=True, ge=1)]

class SourceListItem(SourceSummary):
    has_published_knowledge: bool

class SourceListResponse(StrictAPIModel):
    items: Annotated[list[SourceListItem], Field(max_length=100)]
    has_more: bool
    next_after: Identifier | None

    @model_validator(mode='after')
    def consistent_page(self):
        ids=[item.document_id for item in self.items]
        if ids != sorted(set(ids)) or self.has_more != (self.next_after is not None) or (self.has_more and (not ids or self.next_after != ids[-1])):
            raise ValueError('invalid source pagination')
        return self

class SourceReadResponse(SourceSummary):
    chunk_id: Identifier
    ordinal: Annotated[int, Field(strict=True, ge=0)]
    char_start: Annotated[int, Field(strict=True, ge=0)]
    char_end: Annotated[int, Field(strict=True, ge=1)]
    text: Annotated[str, StringConstraints(strict=True, strip_whitespace=False, min_length=1, max_length=50000)]
    checksum: Checksum
    page_number: Annotated[int, Field(strict=True, ge=1)] | None = None
    section: ShortText | None = None

    @model_validator(mode='after')
    def exact_text(self):
        if self.ordinal >= self.chunk_count or len(self.text) != self.char_end-self.char_start or sha256(self.text.encode()).hexdigest() != self.checksum:
            raise ValueError('invalid source text location or checksum')
        return self
