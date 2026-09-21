"""Bounded source-file validation and import through the ontology boundary."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, StringConstraints, model_validator

from .contracts import Checksum, Identifier, StrictAPIModel


MAX_ONTOLOGY_SOURCE_BYTES = 1_000_000
SOURCE_NORMALIZATION_VERSION = "ontology-source-normalization.v1"


class OntologySourceRequest(StrictAPIModel):
    filename: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=240)]
    content: Annotated[str, StringConstraints(strict=True, strip_whitespace=False, max_length=MAX_ONTOLOGY_SOURCE_BYTES)]
    rule_reference_registry: JsonValue | None = None

    @model_validator(mode="after")
    def bounded_file(self) -> Self:
        if self.filename != self.filename.strip() or any(c in self.filename for c in ("/", "\\")) or any(ord(c) < 32 or ord(c) == 127 for c in self.filename):
            raise ValueError("ontology filename must be a basename")
        if not self.filename.lower().endswith((".yaml", ".yml", ".json")):
            raise ValueError("ontology filename must end with .yaml, .yml or .json")
        try:
            encoded = self.content.encode("utf-8")
        except UnicodeError as error:
            raise ValueError("ontology content must be UTF-8") from error
        if len(encoded) > MAX_ONTOLOGY_SOURCE_BYTES:
            raise ValueError("ontology source exceeds 1,000,000 bytes")
        return self


class OntologySourceImportRequest(OntologySourceRequest):
    expected_source_sha256: Checksum
    expected_normalization_checksum: Checksum
    expected_checksum: Checksum | None = None
    activate: bool = False
    expected_active_tbox_id: Identifier | None = None


class OntologyDiagnostic(StrictAPIModel):
    severity: Literal["error", "warning", "info"]
    code: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)]
    message: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=4096)]
    path: Annotated[str, StringConstraints(strict=True, max_length=4096)] | None = None
    line: Annotated[int, Field(strict=True, ge=1)] | None = None
    column: Annotated[int, Field(strict=True, ge=1)] | None = None


class OntologySourceSummary(StrictAPIModel):
    key: Identifier | None = None
    version: Annotated[int, Field(strict=True, ge=1)] | None = None
    source_version: Annotated[str, StringConstraints(strict=True, max_length=128)] | None = None
    entity_type_count: Annotated[int, Field(strict=True, ge=0)] = 0
    relationship_type_count: Annotated[int, Field(strict=True, ge=0)] = 0
    property_count: Annotated[int, Field(strict=True, ge=0)] = 0


class OntologySourceValidationResponse(StrictAPIModel):
    schema_name: Literal["ontology-source-validation.v1"] = Field(default="ontology-source-validation.v1", alias="schema")
    valid: bool
    diagnostics: Annotated[list[OntologyDiagnostic], Field(max_length=100)]
    summary: OntologySourceSummary
    normalized_definition: dict[str, JsonValue] | None = None
    source_sha256: Checksum
    normalization_checksum: Checksum | None = None
    source_format: Literal["yaml", "json"]
    expected_checksum: Checksum | None = None

    @model_validator(mode="after")
    def consistent_result(self) -> Self:
        if self.valid != (self.normalized_definition is not None and self.normalization_checksum is not None):
            raise ValueError("valid ontology validation requires its normalized definition")
        if self.valid and any(item.severity == "error" for item in self.diagnostics):
            raise ValueError("valid ontology cannot contain error diagnostics")
        return self
