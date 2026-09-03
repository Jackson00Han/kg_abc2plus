"""Immutable, tenant-owned T-Box definitions for the property graph."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from graphrag_prod.domain import tbox_version_id
from graphrag_prod.graph.governance import (
    ASSERTION_FIELDS,
    ENTITY_FIELDS,
    EntityTypeRule,
    GraphGovernancePolicy,
    RelationshipRule,
)


_TYPE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
_TBOX_KEY = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")
_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")


class PropertyDataType(str, Enum):
    STRING = "STRING"
    INTEGER = "INTEGER"
    FLOAT = "FLOAT"
    DECIMAL = "DECIMAL"
    BOOLEAN = "BOOLEAN"
    DATE = "DATE"
    DATETIME = "DATETIME"
    DURATION = "DURATION"
    URI = "URI"
    JSON = "JSON"


class Cardinality(str, Enum):
    ZERO_OR_ONE = "ZERO_OR_ONE"
    ONE = "ONE"
    ZERO_OR_MORE = "ZERO_OR_MORE"
    ONE_OR_MORE = "ONE_OR_MORE"

    @property
    def required(self) -> bool:
        return self in {Cardinality.ONE, Cardinality.ONE_OR_MORE}

    @property
    def single_valued(self) -> bool:
        return self in {Cardinality.ZERO_OR_ONE, Cardinality.ONE}


class TBoxStatus(str, Enum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    RETIRED = "RETIRED"


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    if _CONTROL_CHARACTER.search(normalized):
        raise ValueError(f"{name} must not contain control characters")
    return normalized


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, name)


def _type_name(value: object, name: str) -> str:
    normalized = _required_text(value, name)
    if _TYPE_NAME.fullmatch(normalized) is None:
        raise ValueError(
            f"{name} must start with an ASCII letter and contain only letters, "
            "digits, and underscores"
        )
    return normalized


def _enum_value(enum_type: type[Enum], value: object, name: str) -> Any:
    if isinstance(value, enum_type):
        return value
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    try:
        return enum_type(value.strip().upper())
    except ValueError as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise ValueError(f"{name} must be one of: {allowed}") from exc


def _strict_keys(
    value: Mapping[str, Any],
    *,
    required: frozenset[str],
    optional: frozenset[str],
    object_name: str,
) -> None:
    keys = set(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise ValueError(
            f"{object_name} is missing required fields: {', '.join(sorted(missing))}"
        )
    if unknown:
        raise ValueError(
            f"{object_name} contains unknown fields: {', '.join(sorted(unknown))}"
        )


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} field names must be strings")
    return value


def _sequence(value: object, name: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{name} must be an array")
    return value


def _unique(values: Sequence[str], name: str) -> None:
    normalized = [item.casefold() for item in values]
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{name} must be unique (case-insensitive)")


@dataclass(frozen=True, slots=True)
class PropertyDefinition:
    """One domain property declared on an entity or relationship type."""

    name: str
    datatype: PropertyDataType
    required: bool
    cardinality: Cardinality
    unit: str | None = None
    description: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _type_name(self.name, "property name"))
        object.__setattr__(
            self,
            "datatype",
            _enum_value(PropertyDataType, self.datatype, "property datatype"),
        )
        if not isinstance(self.required, bool):
            raise ValueError("property required must be a boolean")
        object.__setattr__(
            self,
            "cardinality",
            _enum_value(Cardinality, self.cardinality, "property cardinality"),
        )
        unit = _optional_text(self.unit, "property unit")
        if unit is not None:
            if len(unit) > 64:
                raise ValueError("property unit must not exceed 64 characters")
            if self.datatype not in {
                PropertyDataType.INTEGER,
                PropertyDataType.FLOAT,
                PropertyDataType.DECIMAL,
            }:
                raise ValueError("property unit is allowed only for numeric datatypes")
        object.__setattr__(self, "unit", unit)
        object.__setattr__(
            self,
            "description",
            _optional_text(self.description, "property description"),
        )
        if self.required != self.cardinality.required:
            raise ValueError("property required must agree with cardinality minimum")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> PropertyDefinition:
        value = _mapping(value, "property definition")
        _strict_keys(
            value,
            required=frozenset({"name", "datatype", "required", "cardinality"}),
            optional=frozenset({"unit", "description"}),
            object_name="property definition",
        )
        return cls(
            name=value["name"],
            datatype=value["datatype"],
            required=value["required"],
            cardinality=value["cardinality"],
            unit=value.get("unit"),
            description=value.get("description"),
        )

    def to_mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "datatype": self.datatype.value,
            "required": self.required,
            "cardinality": self.cardinality.value,
        }
        if self.unit is not None:
            result["unit"] = self.unit
        if self.description is not None:
            result["description"] = self.description
        return result


@dataclass(frozen=True, slots=True)
class EntityTypeDefinition:
    """A governed entity label plus its identity and property contract."""

    name: str
    canonical_key_namespaces: tuple[str, ...]
    properties: tuple[PropertyDefinition, ...] = ()
    identity_properties: tuple[str, ...] = ()
    description: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _type_name(self.name, "entity type name"))
        namespaces = tuple(
            _required_text(item, "canonical key namespace").casefold()
            for item in self.canonical_key_namespaces
        )
        if not namespaces:
            raise ValueError("entity type requires canonical key namespaces")
        _unique(namespaces, "canonical key namespaces")
        object.__setattr__(self, "canonical_key_namespaces", namespaces)
        properties = tuple(self.properties)
        if any(not isinstance(item, PropertyDefinition) for item in properties):
            raise ValueError("entity properties must contain PropertyDefinition values")
        _unique([item.name for item in properties], "entity property names")
        object.__setattr__(self, "properties", properties)
        identity_properties = tuple(
            _type_name(item, "identity property name")
            for item in self.identity_properties
        )
        _unique(identity_properties, "identity property names")
        property_by_name = {item.name: item for item in properties}
        for name in identity_properties:
            property_definition = property_by_name.get(name)
            if property_definition is None:
                raise ValueError("identity properties must reference declared properties")
            if not property_definition.required or not property_definition.cardinality.single_valued:
                raise ValueError(
                    "identity properties must be required and single-valued"
                )
        object.__setattr__(self, "identity_properties", identity_properties)
        object.__setattr__(
            self,
            "description",
            _optional_text(self.description, "entity type description"),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> EntityTypeDefinition:
        value = _mapping(value, "entity type definition")
        _strict_keys(
            value,
            required=frozenset({"name", "canonical_key_namespaces"}),
            optional=frozenset({"properties", "identity_properties", "description"}),
            object_name="entity type definition",
        )
        return cls(
            name=value["name"],
            canonical_key_namespaces=tuple(
                _sequence(
                    value["canonical_key_namespaces"],
                    "canonical_key_namespaces",
                )
            ),
            properties=tuple(
                PropertyDefinition.from_mapping(_mapping(item, "entity property"))
                for item in _sequence(value.get("properties", ()), "entity properties")
            ),
            identity_properties=tuple(
                _sequence(value.get("identity_properties", ()), "identity_properties")
            ),
            description=value.get("description"),
        )

    def to_mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "canonical_key_namespaces": list(self.canonical_key_namespaces),
            "properties": [item.to_mapping() for item in self.properties],
            "identity_properties": list(self.identity_properties),
        }
        if self.description is not None:
            result["description"] = self.description
        return result


@dataclass(frozen=True, slots=True)
class RelationshipTypeDefinition:
    """One allowed directed relationship pattern in the property graph."""

    name: str
    source_types: tuple[str, ...]
    target_types: tuple[str, ...]
    properties: tuple[PropertyDefinition, ...] = ()
    source_cardinality: Cardinality = Cardinality.ZERO_OR_MORE
    target_cardinality: Cardinality = Cardinality.ZERO_OR_MORE
    description: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _type_name(self.name, "relationship type name"))
        source_types = tuple(
            _type_name(item, "relationship source type") for item in self.source_types
        )
        target_types = tuple(
            _type_name(item, "relationship target type") for item in self.target_types
        )
        if not source_types or not target_types:
            raise ValueError("relationship types require source and target endpoints")
        _unique(source_types, "relationship source types")
        _unique(target_types, "relationship target types")
        object.__setattr__(self, "source_types", source_types)
        object.__setattr__(self, "target_types", target_types)
        properties = tuple(self.properties)
        if any(not isinstance(item, PropertyDefinition) for item in properties):
            raise ValueError(
                "relationship properties must contain PropertyDefinition values"
            )
        _unique([item.name for item in properties], "relationship property names")
        object.__setattr__(self, "properties", properties)
        object.__setattr__(
            self,
            "source_cardinality",
            _enum_value(
                Cardinality,
                self.source_cardinality,
                "relationship source cardinality",
            ),
        )
        object.__setattr__(
            self,
            "target_cardinality",
            _enum_value(
                Cardinality,
                self.target_cardinality,
                "relationship target cardinality",
            ),
        )
        object.__setattr__(
            self,
            "description",
            _optional_text(self.description, "relationship type description"),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> RelationshipTypeDefinition:
        value = _mapping(value, "relationship type definition")
        _strict_keys(
            value,
            required=frozenset({"name", "source_types", "target_types"}),
            optional=frozenset(
                {
                    "properties",
                    "source_cardinality",
                    "target_cardinality",
                    "description",
                }
            ),
            object_name="relationship type definition",
        )
        return cls(
            name=value["name"],
            source_types=tuple(_sequence(value["source_types"], "source_types")),
            target_types=tuple(_sequence(value["target_types"], "target_types")),
            properties=tuple(
                PropertyDefinition.from_mapping(
                    _mapping(item, "relationship property")
                )
                for item in _sequence(
                    value.get("properties", ()), "relationship properties"
                )
            ),
            source_cardinality=value.get(
                "source_cardinality", Cardinality.ZERO_OR_MORE
            ),
            target_cardinality=value.get(
                "target_cardinality", Cardinality.ZERO_OR_MORE
            ),
            description=value.get("description"),
        )

    def to_mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "source_types": list(self.source_types),
            "target_types": list(self.target_types),
            "properties": [item.to_mapping() for item in self.properties],
            "source_cardinality": self.source_cardinality.value,
            "target_cardinality": self.target_cardinality.value,
        }
        if self.description is not None:
            result["description"] = self.description
        return result


@dataclass(frozen=True, slots=True)
class TBoxVersion:
    """One immutable in-memory version of a tenant's property-graph T-Box."""

    tenant_id: str
    key: str
    version: int
    status: TBoxStatus
    entity_types: tuple[EntityTypeDefinition, ...]
    relationship_types: tuple[RelationshipTypeDefinition, ...]
    description: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tenant_id", _required_text(self.tenant_id, "tenant_id"))
        key = _required_text(self.key, "T-Box key")
        if _TBOX_KEY.fullmatch(key) is None:
            raise ValueError(
                "T-Box key must be lowercase and contain only letters, digits, dots, "
                "underscores, and hyphens"
            )
        object.__setattr__(self, "key", key)
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version <= 0
        ):
            raise ValueError("T-Box version must be a positive integer")
        object.__setattr__(
            self,
            "status",
            _enum_value(TBoxStatus, self.status, "T-Box status"),
        )
        entity_types = tuple(self.entity_types)
        relationship_types = tuple(self.relationship_types)
        if not entity_types:
            raise ValueError("T-Box requires at least one entity type")
        if any(not isinstance(item, EntityTypeDefinition) for item in entity_types):
            raise ValueError("entity_types must contain EntityTypeDefinition values")
        if any(
            not isinstance(item, RelationshipTypeDefinition)
            for item in relationship_types
        ):
            raise ValueError(
                "relationship_types must contain RelationshipTypeDefinition values"
            )
        _unique([item.name for item in entity_types], "entity type names")
        _unique([item.name for item in relationship_types], "relationship type names")
        names = {item.name for item in entity_types}
        for relationship in relationship_types:
            unknown = (set(relationship.source_types) | set(relationship.target_types)) - names
            if unknown:
                raise ValueError(
                    "relationship endpoints reference unknown entity types: "
                    + ", ".join(sorted(unknown))
                )
        object.__setattr__(self, "entity_types", entity_types)
        object.__setattr__(self, "relationship_types", relationship_types)
        object.__setattr__(
            self,
            "description",
            _optional_text(self.description, "T-Box description"),
        )

    @property
    def tbox_id(self) -> str:
        return tbox_version_id(self.tenant_id, self.key, self.version)

    @property
    def canonical_definition(self) -> str:
        """Return an order-independent JSON representation of schema content."""
        payload: dict[str, Any] = {
            "description": self.description,
            "entity_types": [
                _canonical_entity(item)
                for item in sorted(self.entity_types, key=lambda item: item.name.casefold())
            ],
            "relationship_types": [
                _canonical_relationship(item)
                for item in sorted(
                    self.relationship_types, key=lambda item: item.name.casefold()
                )
            ],
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.canonical_definition.encode("utf-8")).hexdigest()

    def with_status(self, status: TBoxStatus | str) -> TBoxVersion:
        return dataclasses.replace(
            self,
            status=_enum_value(TBoxStatus, status, "T-Box status"),
        )

    def to_mapping(self, *, include_computed: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "tenant_id": self.tenant_id,
            "key": self.key,
            "version": self.version,
            "status": self.status.value,
            "entity_types": [item.to_mapping() for item in self.entity_types],
            "relationship_types": [
                item.to_mapping() for item in self.relationship_types
            ],
        }
        if self.description is not None:
            result["description"] = self.description
        if include_computed:
            result["tbox_id"] = self.tbox_id
            result["checksum"] = self.checksum
        return result

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> TBoxVersion:
        value = _mapping(value, "T-Box")
        _strict_keys(
            value,
            required=frozenset(
                {
                    "tenant_id",
                    "key",
                    "version",
                    "status",
                    "entity_types",
                    "relationship_types",
                }
            ),
            optional=frozenset({"description", "tbox_id", "checksum"}),
            object_name="T-Box",
        )
        result = cls(
            tenant_id=value["tenant_id"],
            key=value["key"],
            version=value["version"],
            status=value["status"],
            entity_types=tuple(
                EntityTypeDefinition.from_mapping(_mapping(item, "entity type"))
                for item in _sequence(value["entity_types"], "entity_types")
            ),
            relationship_types=tuple(
                RelationshipTypeDefinition.from_mapping(
                    _mapping(item, "relationship type")
                )
                for item in _sequence(
                    value["relationship_types"], "relationship_types"
                )
            ),
            description=value.get("description"),
        )
        if "tbox_id" in value and value["tbox_id"] != result.tbox_id:
            raise ValueError("T-Box tbox_id does not match its identity fields")
        if "checksum" in value and value["checksum"] != result.checksum:
            raise ValueError("T-Box checksum does not match its schema content")
        return result

    @classmethod
    def from_json(cls, value: str | bytes) -> TBoxVersion:
        try:
            payload = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("T-Box JSON is invalid") from exc
        return cls.from_mapping(_mapping(payload, "T-Box"))

    def compile_governance_policy(
        self,
        *,
        minimum_entity_confidence: float = 0.8,
        minimum_assertion_confidence: float = 0.8,
        anomalous_hub_degree: int = 1000,
    ) -> GraphGovernancePolicy:
        """Compile type patterns into the existing ingestion policy contract.

        Domain properties remain governed by this T-Box.  The legacy policy
        fields describe the fixed Entity/Assertion transport models, preserving
        compatibility with existing ingestion callers.
        """
        if not self.relationship_types:
            raise ValueError(
                "a governance policy requires at least one relationship type"
            )
        return GraphGovernancePolicy(
            policy_id=f"{self.tenant_id}:{self.key}:v{self.version}",
            policy_version=self.version,
            entity_rules=tuple(
                EntityTypeRule(
                    entity_type=item.name,
                    canonical_key_namespaces=frozenset(
                        item.canonical_key_namespaces
                    ),
                    required_properties=frozenset(ENTITY_FIELDS),
                    allowed_properties=frozenset(ENTITY_FIELDS),
                )
                for item in self.entity_types
            ),
            relationship_rules=tuple(
                RelationshipRule(
                    predicate=item.name,
                    subject_types=frozenset(item.source_types),
                    object_kind="entity",
                    object_types=frozenset(item.target_types),
                    required_properties=frozenset(ASSERTION_FIELDS),
                    allowed_properties=frozenset(ASSERTION_FIELDS),
                )
                for item in self.relationship_types
            ),
            minimum_entity_confidence=minimum_entity_confidence,
            minimum_assertion_confidence=minimum_assertion_confidence,
            anomalous_hub_degree=anomalous_hub_degree,
        )


def _canonical_property(value: PropertyDefinition) -> dict[str, Any]:
    return value.to_mapping()


def _canonical_entity(value: EntityTypeDefinition) -> dict[str, Any]:
    result = value.to_mapping()
    result["canonical_key_namespaces"] = sorted(value.canonical_key_namespaces)
    result["identity_properties"] = sorted(value.identity_properties)
    result["properties"] = [
        _canonical_property(item)
        for item in sorted(value.properties, key=lambda item: item.name.casefold())
    ]
    return result


def _canonical_relationship(value: RelationshipTypeDefinition) -> dict[str, Any]:
    result = value.to_mapping()
    result["source_types"] = sorted(value.source_types)
    result["target_types"] = sorted(value.target_types)
    result["properties"] = [
        _canonical_property(item)
        for item in sorted(value.properties, key=lambda item: item.name.casefold())
    ]
    return result


def load_tbox(path: Path) -> TBoxVersion:
    return TBoxVersion.from_json(path.read_bytes())
