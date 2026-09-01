"""Versioned graph schema policy and deterministic ingestion governance."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from graphrag_prod.domain.models import Assertion, Entity
from graphrag_prod.graph.provenance import ProvenanceBundle


ENTITY_FIELDS = frozenset(field.name for field in dataclasses.fields(Entity))
ASSERTION_FIELDS = frozenset(field.name for field in dataclasses.fields(Assertion))
_SPACE = re.compile(r"\s+")
_MATCH_PUNCTUATION = re.compile(r"[^\w]+", flags=re.UNICODE)


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {
            field.name: _jsonable(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, (set, frozenset, tuple, list)):
        converted = [_jsonable(item) for item in value]
        return sorted(converted, key=lambda item: json.dumps(item, sort_keys=True))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in sorted(value.items())}
    return value


def normalize_display_name(value: str) -> str:
    """Apply conservative Unicode/whitespace normalization without changing case."""
    normalized = _SPACE.sub(" ", unicodedata.normalize("NFKC", value)).strip()
    if not normalized:
        raise ValueError("entity names and aliases must not be empty")
    return normalized


def normalized_name_key(value: str) -> str:
    """Return a comparison key; it is never sufficient evidence for a merge."""
    display = normalize_display_name(value).casefold()
    return _SPACE.sub(" ", _MATCH_PUNCTUATION.sub(" ", display)).strip()


@dataclass(frozen=True, slots=True)
class EntityTypeRule:
    entity_type: str
    canonical_key_namespaces: frozenset[str]
    required_properties: frozenset[str]
    allowed_properties: frozenset[str]

    def __post_init__(self) -> None:
        if not self.entity_type.strip():
            raise ValueError("entity_type must not be empty")
        namespaces = frozenset(item.strip().casefold() for item in self.canonical_key_namespaces)
        if not namespaces:
            raise ValueError("entity rule requires canonical key namespaces")
        object.__setattr__(self, "canonical_key_namespaces", namespaces)
        if not self.required_properties <= self.allowed_properties:
            raise ValueError("required entity properties must also be allowed")
        if not self.allowed_properties <= ENTITY_FIELDS:
            raise ValueError("entity policy names unknown properties")


@dataclass(frozen=True, slots=True)
class RelationshipRule:
    predicate: str
    subject_types: frozenset[str]
    object_kind: str
    object_types: frozenset[str]
    required_properties: frozenset[str]
    allowed_properties: frozenset[str]

    def __post_init__(self) -> None:
        if not self.predicate.strip():
            raise ValueError("relationship predicate must not be empty")
        if not self.subject_types:
            raise ValueError("relationship rule requires subject types")
        if self.object_kind not in {"entity", "literal"}:
            raise ValueError("relationship object_kind must be entity or literal")
        if self.object_kind == "entity" and not self.object_types:
            raise ValueError("entity-object relationship requires object types")
        if self.object_kind == "literal" and self.object_types:
            raise ValueError("literal relationship cannot declare object types")
        if not self.required_properties <= self.allowed_properties:
            raise ValueError("required assertion properties must also be allowed")
        if not self.allowed_properties <= ASSERTION_FIELDS:
            raise ValueError("relationship policy names unknown properties")


@dataclass(frozen=True, slots=True)
class GovernanceFinding:
    code: str
    action: str
    object_kind: str
    object_id: str
    detail: str


class GovernanceRejected(ValueError):
    """Raised when derived graph data violates the declared schema boundary."""

    def __init__(self, findings: tuple[GovernanceFinding, ...]) -> None:
        self.findings = findings
        summary = "; ".join(f"{item.code}:{item.object_id}" for item in findings)
        super().__init__(f"graph governance rejected derived data: {summary}")


@dataclass(frozen=True, slots=True)
class GovernedBundle:
    bundle: ProvenanceBundle
    findings: tuple[GovernanceFinding, ...]


@dataclass(frozen=True, slots=True)
class GraphGovernancePolicy:
    policy_id: str
    policy_version: int
    entity_rules: tuple[EntityTypeRule, ...]
    relationship_rules: tuple[RelationshipRule, ...]
    minimum_entity_confidence: float
    minimum_assertion_confidence: float
    anomalous_hub_degree: int

    def __post_init__(self) -> None:
        if not self.policy_id.strip():
            raise ValueError("governance policy_id must not be empty")
        if self.policy_version <= 0:
            raise ValueError("governance policy_version must be positive")
        for name, value in (
            ("minimum_entity_confidence", self.minimum_entity_confidence),
            ("minimum_assertion_confidence", self.minimum_assertion_confidence),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between zero and one")
        if self.anomalous_hub_degree <= 0:
            raise ValueError("anomalous_hub_degree must be positive")
        entity_types = [rule.entity_type for rule in self.entity_rules]
        if not entity_types or len(set(entity_types)) != len(entity_types):
            raise ValueError("entity rules must have unique entity types")
        patterns = [
            (rule.predicate, tuple(sorted(rule.subject_types)), rule.object_kind, tuple(sorted(rule.object_types)))
            for rule in self.relationship_rules
        ]
        if not patterns or len(set(patterns)) != len(patterns):
            raise ValueError("relationship patterns must be unique")
        allowed_types = set(entity_types)
        for rule in self.relationship_rules:
            if not rule.subject_types <= allowed_types or not rule.object_types <= allowed_types:
                raise ValueError("relationship pattern references an unknown entity type")

    @property
    def canonical_payload(self) -> str:
        return json.dumps(_jsonable(self), sort_keys=True, separators=(",", ":"))

    @property
    def payload_hash(self) -> str:
        return hashlib.sha256(self.canonical_payload.encode("utf-8")).hexdigest()

    @property
    def entity_rules_by_type(self) -> dict[str, EntityTypeRule]:
        return {rule.entity_type: rule for rule in self.entity_rules}

    def _relationship_rule(
        self,
        assertion: Assertion,
        entities: Mapping[str, Entity],
    ) -> RelationshipRule | None:
        subject = entities.get(assertion.subject_entity_id)
        object_entity = (
            entities.get(assertion.object_entity_id)
            if assertion.object_entity_id is not None
            else None
        )
        object_kind = "entity" if object_entity is not None else "literal"
        for rule in self.relationship_rules:
            if (
                rule.predicate == assertion.predicate
                and subject is not None
                and subject.entity_type in rule.subject_types
                and rule.object_kind == object_kind
                and (
                    object_kind == "literal"
                    or (object_entity is not None and object_entity.entity_type in rule.object_types)
                )
            ):
                return rule
        return None

    def allows_relationship(
        self,
        predicate: str,
        subject_type: str | None,
        object_kind: str,
        object_type: str | None,
    ) -> bool:
        return any(
            rule.predicate == predicate
            and subject_type in rule.subject_types
            and rule.object_kind == object_kind
            and (
                object_kind == "literal"
                or (object_type is not None and object_type in rule.object_types)
            )
            for rule in self.relationship_rules
        )

    def govern_bundle(self, bundle: ProvenanceBundle) -> GovernedBundle:
        """Normalize profiles, reject invalid patterns, and quarantine weak claims."""
        findings: list[GovernanceFinding] = []
        blocked: list[GovernanceFinding] = []
        normalized_entities: list[Entity] = []
        rules = self.entity_rules_by_type

        for entity in bundle.entities:
            rule = rules.get(entity.entity_type)
            if rule is None:
                blocked.append(
                    GovernanceFinding(
                        "ENTITY_TYPE_NOT_ALLOWED", "REJECT", "Entity", entity.entity_id,
                        f"entity type {entity.entity_type!r} is outside policy {self.policy_id}",
                    )
                )
                continue
            namespace = entity.canonical_key.partition(":")[0].casefold()
            if ":" not in entity.canonical_key or namespace not in rule.canonical_key_namespaces:
                blocked.append(
                    GovernanceFinding(
                        "ENTITY_KEY_NAMESPACE_NOT_ALLOWED", "REJECT", "Entity", entity.entity_id,
                        f"canonical key namespace {namespace!r} is not allowed for {entity.entity_type}",
                    )
                )
            canonical_name = normalize_display_name(entity.canonical_name)
            aliases_by_key: dict[str, str] = {}
            for alias in entity.aliases:
                display = normalize_display_name(alias)
                key = normalized_name_key(display)
                if key != normalized_name_key(canonical_name):
                    aliases_by_key.setdefault(key, display)
            normalized = dataclasses.replace(
                entity,
                canonical_name=canonical_name,
                aliases=tuple(sorted(aliases_by_key.values(), key=normalized_name_key)),
            )
            if normalized != entity:
                findings.append(
                    GovernanceFinding(
                        "ENTITY_PROFILE_NORMALIZED", "NORMALIZE", "Entity", entity.entity_id,
                        "canonical name and aliases were Unicode/whitespace normalized and deduplicated",
                    )
                )
            normalized_entities.append(normalized)

        if blocked:
            raise GovernanceRejected(tuple(blocked))

        entities = {entity.entity_id: entity for entity in normalized_entities}
        weak_entities = {
            mention.entity_id
            for mention in bundle.mentions
            if mention.confidence < self.minimum_entity_confidence
        }
        if weak_entities:
            raise GovernanceRejected(
                tuple(
                    GovernanceFinding(
                        "ENTITY_EVIDENCE_BELOW_THRESHOLD", "REJECT", "Entity", entity_id,
                        "all published entities require mention evidence at or above the policy threshold",
                    )
                    for entity_id in sorted(weak_entities)
                )
            )

        governed_assertions: list[Assertion] = []
        for assertion in bundle.all_assertions:
            if self._relationship_rule(assertion, entities) is None:
                blocked.append(
                    GovernanceFinding(
                        "RELATIONSHIP_PATTERN_NOT_ALLOWED", "REJECT", "Assertion", assertion.assertion_id,
                        f"predicate/endpoints are outside policy {self.policy_id}",
                    )
                )
                continue
            governed = assertion
            if assertion.accepted and assertion.confidence < self.minimum_assertion_confidence:
                governed = dataclasses.replace(assertion, accepted=False)
                findings.append(
                    GovernanceFinding(
                        "ASSERTION_BELOW_THRESHOLD", "QUARANTINE", "Assertion", assertion.assertion_id,
                        "claim remains traceable but is excluded from accepted graph navigation",
                    )
                )
            governed_assertions.append(governed)

        if blocked:
            raise GovernanceRejected(tuple(blocked))

        primary = None if bundle.assertion is None else governed_assertions[0]
        additional_start = 0 if bundle.assertion is None else 1
        governed_bundle = dataclasses.replace(
            bundle,
            entities=tuple(normalized_entities),
            assertion=primary,
            additional_assertions=tuple(governed_assertions[additional_start:]),
        )
        return GovernedBundle(governed_bundle, tuple(findings))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> GraphGovernancePolicy:
        entity_properties = value["entity_properties"]
        assertion_properties = value["assertion_properties"]
        return cls(
            policy_id=str(value["policy_id"]),
            policy_version=int(value["policy_version"]),
            entity_rules=tuple(
                EntityTypeRule(
                    entity_type=str(item["entity_type"]),
                    canonical_key_namespaces=frozenset(item["canonical_key_namespaces"]),
                    required_properties=frozenset(entity_properties["required"]),
                    allowed_properties=frozenset(entity_properties["allowed"]),
                )
                for item in value["entity_types"]
            ),
            relationship_rules=tuple(
                RelationshipRule(
                    predicate=str(item["predicate"]),
                    subject_types=frozenset(item["subject_types"]),
                    object_kind=str(item["object_kind"]),
                    object_types=frozenset(item.get("object_types", ())),
                    required_properties=frozenset(assertion_properties["required"]),
                    allowed_properties=frozenset(assertion_properties["allowed"]),
                )
                for item in value["relationships"]
            ),
            minimum_entity_confidence=float(value["minimum_entity_confidence"]),
            minimum_assertion_confidence=float(value["minimum_assertion_confidence"]),
            anomalous_hub_degree=int(value["anomalous_hub_degree"]),
        )


def load_governance_policy(path: Path, policy_id: str) -> GraphGovernancePolicy:
    payload = json.loads(path.read_text(encoding="utf-8"))
    matches = [item for item in payload["policies"] if item["policy_id"] == policy_id]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one governance policy {policy_id!r}")
    return GraphGovernancePolicy.from_mapping(matches[0])
