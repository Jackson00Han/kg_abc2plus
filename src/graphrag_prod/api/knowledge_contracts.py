"""Strict HTTP contracts for governed property-graph knowledge operations.

Tenant identity, principals, and capabilities are reconstructed from a verified
JWT at the application boundary.  Construction callers must select the source
ACL explicitly; the adapter verifies that every selected group belongs to the
authenticated principal.
"""

from __future__ import annotations

import base64
import binascii
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, StringConstraints, field_validator, model_validator

from .contracts import (
    GroupName,
    Identifier,
    LiteralSourceText,
    LiteralTemporalText,
    LiteralUnitText,
    MAX_DOCUMENT_BYTES,
    MAX_GROUPS,
    ShortText,
    StrictAPIModel,
    TypedLiteralSemanticsResponse,
    _canonical_source_uri,
    _json_array,
    _json_aware_datetime,
    _safe_metadata_text,
    _unique,
)


MAX_ONTOLOGY_ENTITY_TYPES = 256
MAX_ONTOLOGY_RELATIONSHIP_TYPES = 512
MAX_ONTOLOGY_PROPERTIES = 256
MAX_KNOWLEDGE_RECORDS = 500
MAX_REVIEW_RECORDS = 100
MAX_PUBLICATION_RECORDS = 500
MAX_EVIDENCE_CHARS = 100_000
MAX_BASE64_DOCUMENT_CHARS = 4 * ((MAX_DOCUMENT_BYTES + 2) // 3)

TypeName = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z][A-Za-z0-9_]*$",
    ),
]
OntologyKey = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9._-]*$",
    ),
]
Digest = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=True,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    ),
]
LongText = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=True,
        min_length=1,
        max_length=4_000,
    ),
]
ExactEvidenceText = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=False,
        min_length=1,
        max_length=MAX_EVIDENCE_CHARS,
    ),
]


class OntologyProperty(StrictAPIModel):
    name: TypeName
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
    required: bool
    cardinality: Literal["ZERO_OR_ONE", "ONE", "ZERO_OR_MORE", "ONE_OR_MORE"]
    unit: Annotated[
        str,
        StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=64),
    ] | None = None
    description: ShortText | None = None


class OntologyEntityType(StrictAPIModel):
    name: TypeName
    canonical_key_namespaces: Annotated[
        tuple[OntologyKey, ...], Field(min_length=1, max_length=64)
    ]
    properties: Annotated[
        tuple[OntologyProperty, ...], Field(max_length=MAX_ONTOLOGY_PROPERTIES)
    ] = ()
    identity_properties: Annotated[
        tuple[TypeName, ...], Field(max_length=64)
    ] = ()
    description: ShortText | None = None

    @field_validator(
        "canonical_key_namespaces", "properties", "identity_properties", mode="before"
    )
    @classmethod
    def accept_json_arrays(cls, value: object) -> object:
        return _json_array(value)

    @model_validator(mode="after")
    def unique_members(self) -> Self:
        _unique(self.canonical_key_namespaces, "canonical_key_namespaces")
        _unique(tuple(item.name for item in self.properties), "properties")
        _unique(self.identity_properties, "identity_properties")
        return self


class OntologyRelationshipType(StrictAPIModel):
    name: TypeName
    source_types: Annotated[tuple[TypeName, ...], Field(min_length=1, max_length=64)]
    target_types: Annotated[tuple[TypeName, ...], Field(min_length=1, max_length=64)]
    properties: Annotated[
        tuple[OntologyProperty, ...], Field(max_length=MAX_ONTOLOGY_PROPERTIES)
    ] = ()
    source_cardinality: Literal[
        "ZERO_OR_ONE", "ONE", "ZERO_OR_MORE", "ONE_OR_MORE"
    ] = "ZERO_OR_MORE"
    target_cardinality: Literal[
        "ZERO_OR_ONE", "ONE", "ZERO_OR_MORE", "ONE_OR_MORE"
    ] = "ZERO_OR_MORE"
    description: ShortText | None = None

    @field_validator("source_types", "target_types", "properties", mode="before")
    @classmethod
    def accept_json_arrays(cls, value: object) -> object:
        return _json_array(value)

    @model_validator(mode="after")
    def unique_members(self) -> Self:
        _unique(self.source_types, "source_types")
        _unique(self.target_types, "target_types")
        _unique(tuple(item.name for item in self.properties), "properties")
        return self


class OntologyImportRequest(StrictAPIModel):
    key: OntologyKey
    version: Annotated[int, Field(strict=True, ge=1, le=2_147_483_647)]
    entity_types: Annotated[
        tuple[OntologyEntityType, ...],
        Field(min_length=1, max_length=MAX_ONTOLOGY_ENTITY_TYPES),
    ]
    relationship_types: Annotated[
        tuple[OntologyRelationshipType, ...],
        Field(max_length=MAX_ONTOLOGY_RELATIONSHIP_TYPES),
    ] = ()
    description: ShortText | None = None
    expected_checksum: Digest | None = None

    @field_validator("entity_types", "relationship_types", mode="before")
    @classmethod
    def accept_json_arrays(cls, value: object) -> object:
        return _json_array(value)

    @model_validator(mode="after")
    def unique_types(self) -> Self:
        _unique(tuple(item.name for item in self.entity_types), "entity_types")
        _unique(tuple(item.name for item in self.relationship_types), "relationship_types")
        return self


class OntologyPublishRequest(StrictAPIModel):
    expected_active_tbox_id: Identifier | None = None


class OntologyListRequest(StrictAPIModel):
    key: OntologyKey | None = None
    status: Literal["DRAFT", "PUBLISHED", "RETIRED"] | None = None
    limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 100


class OntologyVersionResponse(StrictAPIModel):
    tbox_id: Identifier
    key: OntologyKey
    version: Annotated[int, Field(strict=True, ge=1)]
    status: Literal["DRAFT", "PUBLISHED", "RETIRED"]
    checksum: Digest
    entity_types: Annotated[
        tuple[OntologyEntityType, ...], Field(max_length=MAX_ONTOLOGY_ENTITY_TYPES)
    ]
    relationship_types: Annotated[
        tuple[OntologyRelationshipType, ...],
        Field(max_length=MAX_ONTOLOGY_RELATIONSHIP_TYPES),
    ]
    description: ShortText | None = None

    @field_validator("entity_types", "relationship_types", mode="before")
    @classmethod
    def accept_json_arrays(cls, value: object) -> object:
        return _json_array(value)


class OntologyListResponse(StrictAPIModel):
    items: Annotated[tuple[OntologyVersionResponse, ...], Field(max_length=100)]

    @field_validator("items", mode="before")
    @classmethod
    def accept_json_array(cls, value: object) -> object:
        return _json_array(value)


class EvidenceInput(StrictAPIModel):
    document_id: Identifier
    version_id: Identifier
    chunk_id: Identifier
    char_start: Annotated[int, Field(strict=True, ge=0, le=2_147_483_647)]
    char_end: Annotated[int, Field(strict=True, ge=1, le=2_147_483_647)]
    quoted_text: ExactEvidenceText

    @model_validator(mode="after")
    def valid_exact_range(self) -> Self:
        if self.char_end <= self.char_start:
            raise ValueError("evidence range must be non-empty")
        if len(self.quoted_text) != self.char_end - self.char_start:
            raise ValueError("quoted_text length must equal the evidence range")
        return self


class KnowledgeEntityInput(StrictAPIModel):
    entity_type: TypeName
    canonical_key: ShortText
    canonical_name: ShortText
    aliases: Annotated[tuple[ShortText, ...], Field(max_length=100)] = ()

    @field_validator("aliases", mode="before")
    @classmethod
    def accept_json_array(cls, value: object) -> object:
        return _json_array(value)

    @field_validator("aliases")
    @classmethod
    def unique_aliases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "aliases")

    @field_validator("canonical_key", "canonical_name")
    @classmethod
    def safe_identity_text(cls, value: str, info: object) -> str:
        return _safe_metadata_text(value, getattr(info, "field_name", "identity"))


class AuthoritativeMentionInput(StrictAPIModel):
    source_key: Identifier
    expected_previous_revision: Annotated[
        int, Field(strict=True, ge=0, le=2_147_483_646)
    ] = 0
    entity: KnowledgeEntityInput
    evidence: EvidenceInput
    confidence: Annotated[float, Field(strict=True, ge=0.0, le=1.0)] = 1.0


class RawLiteralInput(StrictAPIModel):
    """Untrusted source tokens; canonical semantics are always server-owned."""

    raw_literal: LiteralSourceText
    raw_unit: LiteralUnitText | None = None
    raw_valid_from: LiteralTemporalText | None = None
    raw_valid_to: LiteralTemporalText | None = None
    raw_observed_at: LiteralTemporalText | None = None


class AuthoritativeAssertionInput(StrictAPIModel):
    source_key: Identifier
    expected_previous_revision: Annotated[
        int, Field(strict=True, ge=0, le=2_147_483_646)
    ] = 0
    subject_mention_source_key: Identifier
    predicate: TypeName
    evidence: EvidenceInput
    confidence: Annotated[float, Field(strict=True, ge=0.0, le=1.0)] = 1.0
    object_mention_source_key: Identifier | None = None
    literal: RawLiteralInput | None = None

    @model_validator(mode="after")
    def exactly_one_object(self) -> Self:
        if (self.object_mention_source_key is None) == (self.literal is None):
            raise ValueError("assertion requires exactly one entity or literal object")
        return self


class AuthoritativeImportRequest(StrictAPIModel):
    ontology_version_id: Identifier
    mentions: Annotated[
        tuple[AuthoritativeMentionInput, ...],
        Field(min_length=1, max_length=MAX_KNOWLEDGE_RECORDS),
    ]
    assertions: Annotated[
        tuple[AuthoritativeAssertionInput, ...], Field(max_length=MAX_KNOWLEDGE_RECORDS)
    ] = ()
    review_notes: LongText | None = None

    @field_validator("mentions", "assertions", mode="before")
    @classmethod
    def accept_json_arrays(cls, value: object) -> object:
        return _json_array(value)

    @model_validator(mode="after")
    def validate_batch_references(self) -> Self:
        if len(self.mentions) + len(self.assertions) > MAX_KNOWLEDGE_RECORDS:
            raise ValueError("authoritative import exceeds the record limit")
        mention_keys = tuple(item.source_key for item in self.mentions)
        assertion_keys = tuple(item.source_key for item in self.assertions)
        _unique(mention_keys, "mention source keys")
        _unique(assertion_keys, "assertion source keys")
        if set(mention_keys) & set(assertion_keys):
            raise ValueError("mention and assertion source keys must be distinct")
        available = set(mention_keys)
        for assertion in self.assertions:
            if assertion.subject_mention_source_key not in available:
                raise ValueError("assertion subject mention is absent from the batch")
            if (
                assertion.object_mention_source_key is not None
                and assertion.object_mention_source_key not in available
            ):
                raise ValueError("assertion object mention is absent from the batch")
        return self


class AuthoritativeImportResponse(StrictAPIModel):
    ontology_version_id: Identifier
    mention_count: Annotated[int, Field(strict=True, ge=1, le=MAX_KNOWLEDGE_RECORDS)]
    assertion_count: Annotated[int, Field(strict=True, ge=0, le=MAX_KNOWLEDGE_RECORDS)]
    revision_ids: Annotated[
        tuple[Identifier, ...], Field(min_length=1, max_length=MAX_KNOWLEDGE_RECORDS)
    ]

    @field_validator("revision_ids", mode="before")
    @classmethod
    def accept_json_array(cls, value: object) -> object:
        return _json_array(value)


class KnowledgeConstructionRequest(StrictAPIModel):
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
    mime_type: Literal["text/plain", "text/markdown", "text/csv", "application/json"]
    language: Annotated[
        str,
        StringConstraints(
            strict=True,
            strip_whitespace=True,
            min_length=2,
            max_length=32,
            pattern=r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$",
        ),
    ] = "en"
    tbox_key: OntologyKey
    access_groups: Annotated[
        tuple[GroupName, ...],
        Field(min_length=1, max_length=MAX_GROUPS),
    ]
    published_at: AwareDatetime | None = None
    max_attempts: Annotated[int, Field(strict=True, ge=1, le=10)] = 3
    content_base64: Annotated[
        str,
        StringConstraints(
            strict=True,
            strip_whitespace=False,
            min_length=4,
            max_length=MAX_BASE64_DOCUMENT_CHARS,
            pattern=r"^[A-Za-z0-9+/]*={0,2}$",
        ),
    ]

    @field_validator("canonical_uri")
    @classmethod
    def canonicalize_source_uri(cls, value: str) -> str:
        return _canonical_source_uri(value)

    @field_validator("published_at", mode="before")
    @classmethod
    def accept_json_datetime(cls, value: object) -> object:
        return _json_aware_datetime(value)

    @field_validator("access_groups", mode="before")
    @classmethod
    def accept_json_access_groups(cls, value: object) -> object:
        return _json_array(value)

    @field_validator("access_groups")
    @classmethod
    def unique_access_groups(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "access_groups")

    @field_validator("title", "source_name")
    @classmethod
    def safe_metadata(cls, value: str, info: object) -> str:
        return _safe_metadata_text(value, getattr(info, "field_name", "metadata"))

    @field_validator("content_base64")
    @classmethod
    def canonical_base64(cls, value: str) -> str:
        try:
            decoded = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValueError("content_base64 must be canonical base64") from error
        if not decoded or len(decoded) > MAX_DOCUMENT_BYTES:
            raise ValueError("decoded upload must contain between 1 byte and 5 MiB")
        if base64.b64encode(decoded).decode("ascii") != value:
            raise ValueError("content_base64 must be canonical base64")
        return value

    def decoded_content(self) -> bytes:
        return base64.b64decode(self.content_base64, validate=True)


class ConstructionChunkResponse(StrictAPIModel):
    chunk_id: Identifier
    artifact_id: Identifier
    status: Literal["CANDIDATE", "QUARANTINED", "REJECTED", "EMPTY"]
    finding_codes: Annotated[tuple[Identifier, ...], Field(max_length=1_000)]
    mention_record_ids: Annotated[tuple[Identifier, ...], Field(max_length=1_000)]
    assertion_record_ids: Annotated[tuple[Identifier, ...], Field(max_length=1_000)]
    replayed: bool

    @field_validator(
        "finding_codes", "mention_record_ids", "assertion_record_ids", mode="before"
    )
    @classmethod
    def accept_json_arrays(cls, value: object) -> object:
        return _json_array(value)


class KnowledgeConstructionResponse(StrictAPIModel):
    job_id: Identifier
    document_id: Identifier
    version_id: Identifier
    snapshot_id: Identifier
    tbox_id: Identifier
    chunks: Annotated[
        tuple[ConstructionChunkResponse, ...],
        Field(min_length=1, max_length=10_000),
    ]

    @field_validator("chunks", mode="before")
    @classmethod
    def accept_json_array(cls, value: object) -> object:
        return _json_array(value)


class EntityIdentityResponse(StrictAPIModel):
    entity_id: Identifier
    entity_type: TypeName
    canonical_key: ShortText
    canonical_name: ShortText
    aliases: Annotated[tuple[ShortText, ...], Field(max_length=100)] = ()

    @field_validator("aliases", mode="before")
    @classmethod
    def accept_json_array(cls, value: object) -> object:
        return _json_array(value)


class EvidenceResponse(StrictAPIModel):
    document_id: Identifier
    version_id: Identifier
    chunk_id: Identifier
    char_start: Annotated[int, Field(strict=True, ge=0)]
    char_end: Annotated[int, Field(strict=True, ge=1)]
    quoted_text: ExactEvidenceText


class TrustResponse(StrictAPIModel):
    origin: Literal[
        "EXPERT_IMPORT", "EXPERT_CREATED", "LLM_EXTRACTED", "RULE_DERIVED", "FIXTURE"
    ]
    authority: Literal["AUTHORITATIVE", "SECONDARY"]
    status: Literal[
        "CANDIDATE", "APPROVED", "PUBLISHED", "QUARANTINED", "REJECTED", "SUPERSEDED"
    ]
    ontology_version_id: Identifier
    created_at: AwareDatetime
    extractor_version: ShortText | None = None
    prompt_version: ShortText | None = None
    reviewed_by: Identifier | None = None
    reviewed_at: AwareDatetime | None = None
    review_notes: LongText | None = None


class ReviewRecordResponse(StrictAPIModel):
    record_kind: Literal["ENTITY_MENTION", "ASSERTION"]
    record_id: Identifier
    revision_id: Identifier
    revision: Annotated[int, Field(strict=True, ge=1)]
    confidence: Annotated[float, Field(strict=True, ge=0.0, le=1.0)]
    evidence: EvidenceResponse
    trust: TrustResponse
    entity: EntityIdentityResponse | None = None
    subject: EntityIdentityResponse | None = None
    predicate: TypeName | None = None
    subject_mention_revision_id: Identifier | None = None
    object_entity: EntityIdentityResponse | None = None
    object_mention_revision_id: Identifier | None = None
    literal_value: LiteralSourceText | None = None
    literal_semantics: TypedLiteralSemanticsResponse | None = None

    @model_validator(mode="after")
    def shape_matches_kind(self) -> Self:
        if self.record_kind == "ENTITY_MENTION":
            if self.entity is None or any(
                value is not None
                for value in (
                    self.subject,
                    self.predicate,
                    self.subject_mention_revision_id,
                    self.object_entity,
                    self.object_mention_revision_id,
                    self.literal_value,
                    self.literal_semantics,
                )
            ):
                raise ValueError("entity mention review shape is invalid")
        elif (
            self.entity is not None
            or self.subject is None
            or self.predicate is None
            or self.subject_mention_revision_id is None
        ):
            raise ValueError("assertion review shape is invalid")
        elif (self.object_entity is None) == (self.literal_value is None):
            raise ValueError("assertion review object shape is invalid")
        elif (
            self.object_entity is None
            and self.object_mention_revision_id is not None
        ) or (
            self.object_entity is not None
            and self.object_mention_revision_id is None
        ):
            raise ValueError("assertion review mention linkage is invalid")
        if self.object_entity is not None and self.literal_semantics is not None:
            raise ValueError("entity assertion must not carry literal semantics")
        if (
            self.literal_semantics is not None
            and self.literal_semantics.raw_value != self.literal_value
        ):
            raise ValueError("literal semantics must match literal_value")
        return self


class ReviewQueueRequest(StrictAPIModel):
    statuses: Annotated[
        tuple[Literal["CANDIDATE", "QUARANTINED"], ...], Field(min_length=1, max_length=2)
    ] = ("CANDIDATE", "QUARANTINED")
    limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 100

    @field_validator("statuses", mode="before")
    @classmethod
    def accept_json_array(cls, value: object) -> object:
        return _json_array(value)

    @field_validator("statuses")
    @classmethod
    def unique_statuses(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "statuses")


class ReviewQueueResponse(StrictAPIModel):
    items: Annotated[tuple[ReviewRecordResponse, ...], Field(max_length=100)]

    @field_validator("items", mode="before")
    @classmethod
    def accept_json_array(cls, value: object) -> object:
        return _json_array(value)


class MentionEditInput(StrictAPIModel):
    entity: KnowledgeEntityInput
    confidence: Annotated[float, Field(strict=True, ge=0.0, le=1.0)]


class AssertionEditInput(StrictAPIModel):
    subject: KnowledgeEntityInput
    predicate: TypeName
    subject_mention_revision_id: Identifier
    confidence: Annotated[float, Field(strict=True, ge=0.0, le=1.0)]
    object_entity: KnowledgeEntityInput | None = None
    object_mention_revision_id: Identifier | None = None
    literal: RawLiteralInput | None = None

    @model_validator(mode="after")
    def valid_object(self) -> Self:
        if (self.object_entity is None) == (self.literal is None):
            raise ValueError("assertion edit requires exactly one object shape")
        if self.object_entity is None and self.object_mention_revision_id is not None:
            raise ValueError("literal assertion edit cannot reference an object mention")
        if self.object_entity is not None and self.object_mention_revision_id is None:
            raise ValueError("entity assertion edit requires an object mention")
        return self


class ReviewDecisionInput(StrictAPIModel):
    record_kind: Literal["ENTITY_MENTION", "ASSERTION"]
    record_id: Identifier
    expected_revision: Annotated[int, Field(strict=True, ge=1, le=2_147_483_647)]
    decision: Literal["APPROVED", "REJECTED", "QUARANTINED"]
    notes: LongText
    mention_edit: MentionEditInput | None = None
    assertion_edit: AssertionEditInput | None = None

    @model_validator(mode="after")
    def valid_edit(self) -> Self:
        if self.mention_edit is not None and self.assertion_edit is not None:
            raise ValueError("review decision accepts at most one edit")
        if self.record_kind == "ENTITY_MENTION" and self.assertion_edit is not None:
            raise ValueError("mention review cannot contain assertion_edit")
        if self.record_kind == "ASSERTION" and self.mention_edit is not None:
            raise ValueError("assertion review cannot contain mention_edit")
        return self


class ReviewBatchRequest(StrictAPIModel):
    decisions: Annotated[
        tuple[ReviewDecisionInput, ...], Field(min_length=1, max_length=MAX_REVIEW_RECORDS)
    ]

    @field_validator("decisions", mode="before")
    @classmethod
    def accept_json_array(cls, value: object) -> object:
        return _json_array(value)

    @field_validator("decisions")
    @classmethod
    def unique_records(
        cls, value: tuple[ReviewDecisionInput, ...]
    ) -> tuple[ReviewDecisionInput, ...]:
        _unique(tuple(item.record_id for item in value), "review record IDs")
        return value


class ReviewOutcomeResponse(StrictAPIModel):
    record_kind: Literal["ENTITY_MENTION", "ASSERTION"]
    record_id: Identifier
    previous_revision_id: Identifier
    revision_id: Identifier
    revision: Annotated[int, Field(strict=True, ge=2)]
    status: Literal["APPROVED", "REJECTED", "QUARANTINED"]


class ReviewBatchResponse(StrictAPIModel):
    outcomes: Annotated[
        tuple[ReviewOutcomeResponse, ...], Field(min_length=1, max_length=MAX_REVIEW_RECORDS)
    ]

    @field_validator("outcomes", mode="before")
    @classmethod
    def accept_json_array(cls, value: object) -> object:
        return _json_array(value)


class PublicationRequest(StrictAPIModel):
    approved_revision_ids: Annotated[
        tuple[Identifier, ...], Field(max_length=MAX_PUBLICATION_RECORDS)
    ] = ()
    expected_active_publication_id: Identifier | None = None
    remove_record_ids: Annotated[
        tuple[Identifier, ...], Field(max_length=MAX_PUBLICATION_RECORDS)
    ] = ()
    replace_record_ids: Annotated[
        tuple[Identifier, ...], Field(max_length=MAX_PUBLICATION_RECORDS)
    ] = ()

    @field_validator(
        "approved_revision_ids", "remove_record_ids", "replace_record_ids", mode="before"
    )
    @classmethod
    def accept_json_arrays(cls, value: object) -> object:
        return _json_array(value)

    @model_validator(mode="after")
    def valid_change_set(self) -> Self:
        for name in (
            "approved_revision_ids",
            "remove_record_ids",
            "replace_record_ids",
        ):
            _unique(getattr(self, name), name)
        if not (
            self.approved_revision_ids or self.remove_record_ids or self.replace_record_ids
        ):
            raise ValueError("publication change set must not be empty")
        if set(self.remove_record_ids) & set(self.replace_record_ids):
            raise ValueError("record cannot be removed and replaced together")
        if self.replace_record_ids and not self.approved_revision_ids:
            raise ValueError("replacement requires approved revisions")
        return self


class RollbackRequest(StrictAPIModel):
    expected_active_publication_id: Identifier


class PublicationHistoryRequest(StrictAPIModel):
    limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 100


class PublicationResponse(StrictAPIModel):
    publication_id: Identifier
    ontology_version_id: Identifier
    generation: Annotated[int, Field(strict=True, ge=1)]
    manifest_hash: Digest
    source_revision_ids: Annotated[
        tuple[Identifier, ...], Field(max_length=MAX_PUBLICATION_RECORDS)
    ]
    published_revision_ids: Annotated[
        tuple[Identifier, ...], Field(max_length=MAX_PUBLICATION_RECORDS)
    ]
    removed_record_ids: Annotated[
        tuple[Identifier, ...], Field(max_length=MAX_PUBLICATION_RECORDS)
    ]
    replaced_record_ids: Annotated[
        tuple[Identifier, ...], Field(max_length=MAX_PUBLICATION_RECORDS)
    ]
    status: ShortText
    created_by: Identifier
    created_at: AwareDatetime
    activated_at: AwareDatetime | None
    rolled_back_by: Identifier | None = None
    rolled_back_at: AwareDatetime | None = None

    @field_validator(
        "source_revision_ids",
        "published_revision_ids",
        "removed_record_ids",
        "replaced_record_ids",
        mode="before",
    )
    @classmethod
    def accept_json_arrays(cls, value: object) -> object:
        return _json_array(value)


class PublicationHistoryResponse(StrictAPIModel):
    items: Annotated[tuple[PublicationResponse, ...], Field(max_length=100)]

    @field_validator("items", mode="before")
    @classmethod
    def accept_json_array(cls, value: object) -> object:
        return _json_array(value)


__all__ = [
    "AuthoritativeImportRequest",
    "AuthoritativeImportResponse",
    "ConstructionChunkResponse",
    "EvidenceInput",
    "KnowledgeConstructionRequest",
    "KnowledgeConstructionResponse",
    "MAX_BASE64_DOCUMENT_CHARS",
    "OntologyImportRequest",
    "OntologyListRequest",
    "OntologyListResponse",
    "OntologyPublishRequest",
    "OntologyVersionResponse",
    "PublicationHistoryRequest",
    "PublicationHistoryResponse",
    "PublicationRequest",
    "PublicationResponse",
    "RawLiteralInput",
    "ReviewBatchRequest",
    "ReviewBatchResponse",
    "ReviewQueueRequest",
    "ReviewQueueResponse",
    "RollbackRequest",
]
