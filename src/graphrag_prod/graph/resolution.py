"""Conservative, evidence-bearing entity-resolution decisions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum

from .governance import normalize_display_name, normalized_name_key


@dataclass(frozen=True, slots=True, order=True)
class AuthoritativeIdentifier:
    namespace: str
    value: str

    def __post_init__(self) -> None:
        namespace = self.namespace.strip().casefold()
        value = self.value.strip().casefold()
        if not namespace or not value:
            raise ValueError("authoritative identifiers require namespace and value")
        object.__setattr__(self, "namespace", namespace)
        object.__setattr__(self, "value", value)


@dataclass(frozen=True, slots=True)
class ResolutionCandidate:
    candidate_id: str
    tenant_id: str
    entity_type: str
    canonical_name: str
    aliases: tuple[str, ...]
    identifiers: tuple[AuthoritativeIdentifier, ...]
    evidence_mention_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("candidate_id", "tenant_id", "entity_type"):
            value = getattr(self, name).strip()
            if not value:
                raise ValueError(f"{name} must not be empty")
            object.__setattr__(self, name, value)
        object.__setattr__(self, "canonical_name", normalize_display_name(self.canonical_name))
        aliases = {
            normalize_display_name(value)
            for value in self.aliases
            if normalized_name_key(value) != normalized_name_key(self.canonical_name)
        }
        object.__setattr__(self, "aliases", tuple(sorted(aliases, key=normalized_name_key)))
        identifiers = tuple(sorted(set(self.identifiers)))
        if len({item.namespace for item in identifiers}) != len(identifiers):
            raise ValueError(
                "a resolution candidate cannot carry conflicting values in one namespace"
            )
        object.__setattr__(self, "identifiers", identifiers)
        evidence = tuple(sorted({value.strip() for value in self.evidence_mention_ids if value.strip()}))
        if not evidence:
            raise ValueError("resolution candidates require mention evidence")
        object.__setattr__(self, "evidence_mention_ids", evidence)


class ResolutionOutcome(StrEnum):
    MERGE = "MERGE"
    KEEP_SEPARATE = "KEEP_SEPARATE"
    HUMAN_REVIEW = "HUMAN_REVIEW"


@dataclass(frozen=True, slots=True)
class ResolutionDecision:
    decision_id: str
    left_candidate_id: str
    right_candidate_id: str
    outcome: ResolutionOutcome
    rule_id: str
    evidence_mention_ids: tuple[str, ...]
    rationale: str


def _decision(
    left: ResolutionCandidate,
    right: ResolutionCandidate,
    outcome: ResolutionOutcome,
    rule_id: str,
    rationale: str,
) -> ResolutionDecision:
    candidate_ids = tuple(sorted((left.candidate_id, right.candidate_id)))
    evidence = tuple(sorted(set(left.evidence_mention_ids + right.evidence_mention_ids)))
    payload = json.dumps(
        {
            "candidates": candidate_ids,
            "outcome": outcome.value,
            "rule_id": rule_id,
            "evidence": evidence,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return ResolutionDecision(
        decision_id=f"resolution:{hashlib.sha256(payload).hexdigest()}",
        left_candidate_id=candidate_ids[0],
        right_candidate_id=candidate_ids[1],
        outcome=outcome,
        rule_id=rule_id,
        evidence_mention_ids=evidence,
        rationale=rationale,
    )


def resolve_entity_pair(
    left: ResolutionCandidate,
    right: ResolutionCandidate,
) -> ResolutionDecision:
    """Resolve only on authoritative evidence; names alone never auto-merge."""
    if left.candidate_id == right.candidate_id:
        raise ValueError("resolution requires two distinct candidates")
    if left.tenant_id != right.tenant_id:
        return _decision(
            left,
            right,
            ResolutionOutcome.KEEP_SEPARATE,
            "tenant-boundary:v1",
            "entities from different tenants cannot share a governed identity",
        )
    if left.entity_type != right.entity_type:
        return _decision(
            left,
            right,
            ResolutionOutcome.KEEP_SEPARATE,
            "entity-type-boundary:v1",
            "entities with different governed types cannot merge",
        )

    left_by_namespace = {item.namespace: item.value for item in left.identifiers}
    right_by_namespace = {item.namespace: item.value for item in right.identifiers}
    conflicting = sorted(
        namespace
        for namespace in set(left_by_namespace) & set(right_by_namespace)
        if left_by_namespace[namespace] != right_by_namespace[namespace]
    )
    if conflicting:
        return _decision(
            left,
            right,
            ResolutionOutcome.KEEP_SEPARATE,
            "conflicting-authoritative-identifier:v1",
            f"authoritative identifiers conflict in namespaces: {', '.join(conflicting)}",
        )

    shared = sorted(set(left.identifiers) & set(right.identifiers))
    if shared:
        identifiers = ", ".join(f"{item.namespace}:{item.value}" for item in shared)
        return _decision(
            left,
            right,
            ResolutionOutcome.MERGE,
            "exact-authoritative-identifier:v1",
            f"the candidates share authoritative identifier {identifiers}",
        )

    left_names = {normalized_name_key(left.canonical_name), *(normalized_name_key(item) for item in left.aliases)}
    right_names = {normalized_name_key(right.canonical_name), *(normalized_name_key(item) for item in right.aliases)}
    if left_names & right_names:
        return _decision(
            left,
            right,
            ResolutionOutcome.HUMAN_REVIEW,
            "name-only-homonym-guard:v1",
            "a normalized name overlaps, but names alone cannot distinguish homonyms",
        )
    return _decision(
        left,
        right,
        ResolutionOutcome.KEEP_SEPARATE,
        "no-shared-identity-evidence:v1",
        "the candidates share no authoritative identifier or normalized name",
    )
