"""Immutable domain records for source-to-assertion provenance."""

from __future__ import annotations

import json
import math
import re
import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime
from numbers import Real

from .ids import (
    chunk_embedding_id,
    content_checksum,
    embedding_space_id,
    knowledge_snapshot_id,
    pipeline_profile_id,
)


def _text(value: str, name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    return normalized


def _exact_text(value: str, name: str) -> str:
    if value == "":
        raise ValueError(f"{name} must not be empty")
    return value


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _optional_aware(value: datetime | None, name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime or None")
    return _aware(value, name)


def _groups(values: frozenset[str]) -> frozenset[str]:
    normalized = frozenset(value.strip() for value in values if value.strip())
    if not normalized:
        raise ValueError("access_groups must not be empty (use an explicit public group)")
    return normalized


def _sha256(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise ValueError("checksum must be a hexadecimal SHA-256 digest")
    return normalized


@dataclass(frozen=True, slots=True)
class Document:
    document_id: str
    tenant_id: str
    canonical_uri: str
    title: str
    source_name: str
    access_policy_id: str
    access_policy_version: int
    access_groups: frozenset[str]
    created_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "document_id",
            "tenant_id",
            "canonical_uri",
            "title",
            "source_name",
            "access_policy_id",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.access_policy_version <= 0:
            raise ValueError("access_policy_version must be positive")
        object.__setattr__(self, "access_groups", _groups(self.access_groups))
        _aware(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class DocumentVersion:
    version_id: str
    document_id: str
    tenant_id: str
    checksum: str
    original_checksum: str
    normalized_text: str
    version_number: int
    mime_type: str
    language: str
    published_at: datetime | None
    ingested_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "version_id",
            "document_id",
            "tenant_id",
            "mime_type",
            "language",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(
            self,
            "normalized_text",
            _exact_text(self.normalized_text, "normalized_text"),
        )
        object.__setattr__(self, "checksum", _sha256(self.checksum))
        object.__setattr__(self, "original_checksum", _sha256(self.original_checksum))
        if content_checksum(self.normalized_text) != self.checksum:
            raise ValueError("checksum must match normalized_text")
        if self.version_number <= 0:
            raise ValueError("version_number must be positive")
        if self.published_at is not None:
            _aware(self.published_at, "published_at")
        _aware(self.ingested_at, "ingested_at")


@dataclass(frozen=True, slots=True)
class Chunk:
    chunk_id: str
    version_id: str
    document_id: str
    tenant_id: str
    access_policy_id: str
    access_policy_version: int
    access_groups: frozenset[str]
    ordinal: int
    text: str
    checksum: str
    char_start: int
    char_end: int
    page_number: int | None
    section: str | None
    splitter_version: str

    def __post_init__(self) -> None:
        for name in (
            "chunk_id",
            "version_id",
            "document_id",
            "tenant_id",
            "access_policy_id",
            "splitter_version",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(self, "text", _exact_text(self.text, "text"))
        object.__setattr__(self, "checksum", _sha256(self.checksum))
        object.__setattr__(self, "access_groups", _groups(self.access_groups))
        if self.access_policy_version <= 0:
            raise ValueError("access_policy_version must be positive")
        if self.ordinal < 0:
            raise ValueError("ordinal must not be negative")
        if self.char_start < 0 or self.char_end <= self.char_start:
            raise ValueError("chunk character range is invalid")
        if len(self.text) != self.char_end - self.char_start:
            raise ValueError("chunk text length must match its character range")
        if content_checksum(self.text) != self.checksum:
            raise ValueError("chunk checksum must match text")
        if self.page_number is not None and self.page_number <= 0:
            raise ValueError("page_number must be positive")


@dataclass(frozen=True, slots=True)
class Entity:
    entity_id: str
    tenant_id: str
    entity_type: str
    canonical_key: str
    canonical_name: str
    aliases: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        for name in (
            "entity_id",
            "tenant_id",
            "entity_type",
            "canonical_key",
            "canonical_name",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        aliases = tuple(sorted({_text(alias, "alias") for alias in self.aliases}))
        object.__setattr__(self, "aliases", aliases)


@dataclass(frozen=True, slots=True)
class EntityMention:
    mention_id: str
    tenant_id: str
    chunk_id: str
    entity_id: str
    entity_type: str
    surface: str
    char_start: int
    char_end: int
    extractor_version: str
    confidence: float

    def __post_init__(self) -> None:
        for name in (
            "mention_id",
            "tenant_id",
            "chunk_id",
            "entity_id",
            "entity_type",
            "extractor_version",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(self, "surface", _exact_text(self.surface, "surface"))
        if self.char_start < 0 or self.char_end <= self.char_start:
            raise ValueError("mention character range is invalid")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between zero and one")


@dataclass(frozen=True, slots=True)
class GraphPipelineProfile:
    profile_id: str
    normalizer_signature: str
    splitter_signature: str
    extractor_signature: str
    prompt_signature: str
    schema_signature: str
    code_signature: str

    def __post_init__(self) -> None:
        for name in (
            "profile_id",
            "normalizer_signature",
            "splitter_signature",
            "extractor_signature",
            "prompt_signature",
            "schema_signature",
            "code_signature",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        expected_id = pipeline_profile_id(
            self.normalizer_signature,
            self.splitter_signature,
            self.extractor_signature,
            self.prompt_signature,
            self.schema_signature,
            self.code_signature,
        )
        if self.profile_id != expected_id:
            raise ValueError("profile_id does not match its pipeline signatures")


@dataclass(frozen=True, slots=True)
class KnowledgeSnapshot:
    snapshot_id: str
    tenant_id: str
    document_id: str
    version_id: str
    profile_id: str
    manifest_hash: str
    expected_chunk_count: int
    created_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "snapshot_id",
            "tenant_id",
            "document_id",
            "version_id",
            "profile_id",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(self, "manifest_hash", _sha256(self.manifest_hash))
        if isinstance(self.expected_chunk_count, bool) or not isinstance(
            self.expected_chunk_count, int
        ):
            raise ValueError("expected_chunk_count must be a positive integer")
        if self.expected_chunk_count <= 0:
            raise ValueError("expected_chunk_count must be a positive integer")
        if knowledge_snapshot_id(self.version_id, self.profile_id) != self.snapshot_id:
            raise ValueError("snapshot_id does not match version_id and profile_id")
        _aware(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class ChunkEmbedding:
    embedding_id: str
    tenant_id: str
    chunk_id: str
    embedding_space_id: str
    provider: str
    model: str
    revision: str
    dimensions: int
    normalization: str
    created_at: datetime
    vector: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "embedding_id",
            "tenant_id",
            "chunk_id",
            "embedding_space_id",
            "provider",
            "model",
            "revision",
            "normalization",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.dimensions <= 0:
            raise ValueError("dimensions must be positive")
        if (
            embedding_space_id(
                self.provider,
                self.model,
                self.revision,
                self.dimensions,
                self.normalization,
            )
            != self.embedding_space_id
        ):
            raise ValueError("embedding_space_id does not match its profile")
        if (
            chunk_embedding_id(self.chunk_id, self.embedding_space_id)
            != self.embedding_id
        ):
            raise ValueError("embedding_id does not match chunk and vector space")
        try:
            raw_vector = tuple(self.vector)
        except TypeError as error:
            raise ValueError("vector values must be finite numbers") from error
        if any(
            isinstance(value, bool) or not isinstance(value, Real)
            for value in raw_vector
        ):
            raise ValueError("vector values must be finite numbers")
        vector = tuple(float(value) for value in raw_vector)
        if vector and len(vector) != self.dimensions:
            raise ValueError("non-empty vector length must match dimensions")
        if any(not math.isfinite(value) for value in vector):
            raise ValueError("vector values must be finite numbers")
        if vector:
            try:
                float32_vector = tuple(
                    struct.unpack("!f", struct.pack("!f", value))[0]
                    for value in vector
                )
                float32_norm = struct.unpack(
                    "!f",
                    struct.pack("!f", math.hypot(*float32_vector)),
                )[0]
            except (OverflowError, struct.error) as error:
                raise ValueError(
                    "vector and its norm must be representable as float32"
                ) from error
            if not math.isfinite(float32_norm) or float32_norm == 0.0:
                raise ValueError(
                    "vector must have a non-zero finite norm for cosine indexing"
                )
        object.__setattr__(self, "vector", vector)
        _aware(self.created_at, "created_at")

    @property
    def vector_checksum(self) -> str | None:
        """Return a stable checksum when vector values are stored inline."""
        if not self.vector:
            return None
        payload = json.dumps(
            self.vector,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
            sort_keys=True,
        )
        return content_checksum(payload)


_LITERAL_DATATYPES = frozenset(
    {
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
    }
)
_NUMERIC_LITERAL_DATATYPES = frozenset({"INTEGER", "FLOAT", "DECIMAL"})
_RFC3339_LITERAL = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


@dataclass(frozen=True, slots=True)
class TypedLiteralValue:
    """Immutable typed and canonical semantics for one source literal token.

    Neo4j adapters flatten these fields onto Assertion nodes.  The canonical
    value is always a deterministic string so precise decimals and temporal
    values never depend on a driver's lossy scalar coercion.
    """

    datatype: str
    typed_value: str | int | float | bool
    raw_value: str
    canonical_value: str
    raw_unit: str | None = None
    canonical_unit: str | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    observed_at: datetime | None = None
    raw_valid_from: str | None = None
    raw_valid_to: str | None = None
    raw_observed_at: str | None = None

    def __post_init__(self) -> None:
        datatype = _text(self.datatype, "literal datatype").upper()
        if datatype not in _LITERAL_DATATYPES:
            raise ValueError("literal datatype is unsupported")
        object.__setattr__(self, "datatype", datatype)
        if not isinstance(self.raw_value, str):
            raise TypeError("raw_value must be a string")
        if not isinstance(self.canonical_value, str):
            raise TypeError("canonical_value must be a string")
        object.__setattr__(self, "raw_value", _exact_text(self.raw_value, "raw_value"))
        object.__setattr__(
            self,
            "canonical_value",
            _exact_text(self.canonical_value, "canonical_value"),
        )

        typed = self.typed_value
        if datatype == "BOOLEAN":
            valid_typed = isinstance(typed, bool)
        elif datatype == "INTEGER":
            valid_typed = isinstance(typed, int) and not isinstance(typed, bool)
        elif datatype == "FLOAT":
            valid_typed = (
                isinstance(typed, float)
                and math.isfinite(typed)
            )
        else:
            valid_typed = isinstance(typed, str) and bool(typed)
        if not valid_typed:
            raise TypeError(f"typed_value is incompatible with {datatype}")
        if datatype == "INTEGER" and self.canonical_value != str(typed):
            raise ValueError("INTEGER canonical_value must match typed_value")
        if datatype == "FLOAT":
            try:
                canonical_float = float(self.canonical_value)
            except ValueError as exc:
                raise ValueError("FLOAT canonical_value must be numeric") from exc
            if not math.isfinite(canonical_float) or canonical_float != typed:
                raise ValueError("FLOAT canonical_value must match typed_value")
        if datatype == "DECIMAL" and self.canonical_value != typed:
            raise ValueError("DECIMAL canonical_value must match typed_value")
        if datatype == "BOOLEAN" and self.canonical_value != str(typed).lower():
            raise ValueError("BOOLEAN canonical_value must match typed_value")
        if datatype in {"DATE", "DATETIME", "DURATION", "URI", "JSON"} and (
            self.canonical_value != typed
        ):
            raise ValueError(f"{datatype} canonical_value must match typed_value")

        raw_unit = self.raw_unit
        canonical_unit = self.canonical_unit
        if (raw_unit is None) != (canonical_unit is None):
            raise ValueError("raw_unit and canonical_unit must either both exist or both be absent")
        if raw_unit is not None:
            if datatype not in _NUMERIC_LITERAL_DATATYPES:
                raise ValueError("units are allowed only for numeric typed literals")
            if not isinstance(raw_unit, str) or not isinstance(canonical_unit, str):
                raise TypeError("literal units must be strings")
            if raw_unit != raw_unit.strip() or canonical_unit != canonical_unit.strip():
                raise ValueError("literal units must not contain edge whitespace")
            object.__setattr__(self, "raw_unit", _exact_text(raw_unit, "raw_unit"))
            object.__setattr__(
                self,
                "canonical_unit",
                _text(canonical_unit or "", "canonical_unit"),
            )

        valid_from = _optional_aware(self.valid_from, "valid_from")
        valid_to = _optional_aware(self.valid_to, "valid_to")
        observed_at = _optional_aware(self.observed_at, "observed_at")
        if valid_from is not None and valid_to is not None and valid_from >= valid_to:
            raise ValueError("valid_from must be earlier than valid_to")
        for name, value in (
            ("valid_from", valid_from),
            ("valid_to", valid_to),
            ("observed_at", observed_at),
        ):
            if value is not None:
                object.__setattr__(self, name, value.astimezone(UTC))
        for raw_name, parsed in (
            ("raw_valid_from", valid_from),
            ("raw_valid_to", valid_to),
            ("raw_observed_at", observed_at),
        ):
            raw = getattr(self, raw_name)
            if (raw is None) != (parsed is None):
                raise ValueError(f"{raw_name} must accompany its parsed temporal value")
            if raw is not None:
                if not isinstance(raw, str):
                    raise TypeError(f"{raw_name} must be a string or None")
                object.__setattr__(self, raw_name, _exact_text(raw, raw_name))
                if _RFC3339_LITERAL.fullmatch(raw) is None:
                    raise ValueError(f"{raw_name} must be exact RFC3339 text")
                raw_parsed = self._parse_instant(raw, raw_name)
                if raw_parsed is None or raw_parsed.astimezone(UTC) != parsed:
                    raise ValueError(
                        f"{raw_name} does not match its parsed temporal value"
                    )

    @property
    def identity_reference(self) -> str:
        """Canonical JSON included in stable Assertion identity inputs."""

        return json.dumps(
            self.to_mapping(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "datatype": self.datatype,
            "typed_value": self.typed_value,
            "raw_value": self.raw_value,
            "raw_unit": self.raw_unit,
            "canonical_value": self.canonical_value,
            "canonical_unit": self.canonical_unit,
            "valid_from": self._instant_text(self.valid_from),
            "valid_to": self._instant_text(self.valid_to),
            "observed_at": self._instant_text(self.observed_at),
            "raw_valid_from": self.raw_valid_from,
            "raw_valid_to": self.raw_valid_to,
            "raw_observed_at": self.raw_observed_at,
        }

    def to_flat_properties(self, prefix: str = "literal_") -> dict[str, object]:
        """Return Neo4j-safe scalar properties; absent optionals are omitted."""

        return {
            f"{prefix}{key}": value
            for key, value in self.to_mapping().items()
            if value is not None
        }

    @classmethod
    def from_mapping(cls, value: object) -> TypedLiteralValue:
        if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
            raise TypeError("typed literal mapping must be an object")
        required = {
            "datatype",
            "typed_value",
            "raw_value",
            "raw_unit",
            "canonical_value",
            "canonical_unit",
            "valid_from",
            "valid_to",
            "observed_at",
            "raw_valid_from",
            "raw_valid_to",
            "raw_observed_at",
        }
        if set(value) != required:
            raise ValueError("typed literal mapping fields do not match the contract")
        return cls(
            datatype=value["datatype"],  # type: ignore[arg-type]
            typed_value=value["typed_value"],  # type: ignore[arg-type]
            raw_value=value["raw_value"],  # type: ignore[arg-type]
            raw_unit=value["raw_unit"],  # type: ignore[arg-type]
            canonical_value=value["canonical_value"],  # type: ignore[arg-type]
            canonical_unit=value["canonical_unit"],  # type: ignore[arg-type]
            valid_from=cls._parse_instant(value["valid_from"], "valid_from"),
            valid_to=cls._parse_instant(value["valid_to"], "valid_to"),
            observed_at=cls._parse_instant(value["observed_at"], "observed_at"),
            raw_valid_from=value["raw_valid_from"],  # type: ignore[arg-type]
            raw_valid_to=value["raw_valid_to"],  # type: ignore[arg-type]
            raw_observed_at=value["raw_observed_at"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_flat_properties(
        cls,
        properties: object,
        prefix: str = "literal_",
    ) -> TypedLiteralValue | None:
        """Reconstruct a typed literal from flat Neo4j scalar properties.

        Legacy records have no typed-literal properties and return ``None``.
        A partially present group is corruption and is rejected rather than
        silently degrading a typed fact into an untyped legacy literal.
        """

        if not isinstance(properties, dict) or any(
            not isinstance(key, str) for key in properties
        ):
            raise TypeError("flat typed literal properties must be an object")
        fields = (
            "datatype",
            "typed_value",
            "raw_value",
            "raw_unit",
            "canonical_value",
            "canonical_unit",
            "valid_from",
            "valid_to",
            "observed_at",
            "raw_valid_from",
            "raw_valid_to",
            "raw_observed_at",
        )
        present = {
            field for field in fields if f"{prefix}{field}" in properties
        }
        if not present:
            return None
        required = {"datatype", "typed_value", "raw_value", "canonical_value"}
        missing = required - present
        if missing:
            raise ValueError(
                "flat typed literal is missing required fields: "
                + ", ".join(sorted(missing))
            )
        value = {
            field: properties.get(f"{prefix}{field}")
            for field in fields
        }
        return cls.from_mapping(value)

    @staticmethod
    def _instant_text(value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _parse_instant(value: object, name: str) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError(f"typed literal {name} must be a string or None")
        try:
            parsed = datetime.fromisoformat(
                value[:-1] + "+00:00" if value.endswith("Z") else value
            )
        except ValueError as exc:
            raise ValueError(f"typed literal {name} is invalid") from exc
        return _aware(parsed, name)


@dataclass(frozen=True, slots=True)
class Assertion:
    assertion_id: str
    tenant_id: str
    subject_entity_id: str
    predicate: str
    evidence_chunk_id: str
    evidence_char_start: int
    evidence_char_end: int
    extractor_version: str
    schema_version: str
    confidence: float
    accepted: bool
    object_entity_id: str | None = None
    literal_value: str | None = None
    literal_semantics: TypedLiteralValue | None = None

    def __post_init__(self) -> None:
        for name in (
            "assertion_id",
            "tenant_id",
            "subject_entity_id",
            "predicate",
            "evidence_chunk_id",
            "extractor_version",
            "schema_version",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.evidence_char_start < 0 or self.evidence_char_end <= self.evidence_char_start:
            raise ValueError("assertion evidence range is invalid")
        has_entity = self.object_entity_id is not None
        has_literal = self.literal_value is not None
        if has_entity == has_literal:
            raise ValueError("assertion requires exactly one entity or literal object")
        if self.object_entity_id is not None:
            if self.literal_semantics is not None:
                raise ValueError("entity assertion must not carry literal semantics")
            object.__setattr__(
                self,
                "object_entity_id",
                _text(self.object_entity_id, "object_entity_id"),
            )
        if self.literal_value is not None:
            object.__setattr__(
                self,
                "literal_value",
                _text(self.literal_value, "literal_value"),
            )
            if self.literal_semantics is not None:
                if not isinstance(self.literal_semantics, TypedLiteralValue):
                    raise TypeError("literal_semantics must be TypedLiteralValue")
                if self.literal_semantics.raw_value != self.literal_value:
                    raise ValueError("literal_value must equal typed raw_value")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between zero and one")

    @property
    def object_reference(self) -> str:
        if self.object_entity_id is not None:
            return self.object_entity_id
        if self.literal_semantics is not None:
            return self.literal_semantics.identity_reference
        return self.literal_value or ""
