"""Conservative, auditable resolution against authoritative Entity identities.

Resolution produces proposals only.  It never rewires mentions, assertions, or
canonical Entity nodes; applying a proposed link remains an explicit review
decision in the governed revision workflow.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum
import math
import re
from typing import Any, Protocol

from graphrag_prod.domain.access import Principal
from graphrag_prod.graph.governance import normalized_name_key
from graphrag_prod.ontology.models import TBoxStatus, TBoxVersion

from .models import EntityIdentity
from .trust import AuthorityLevel, GovernanceStatus


MAX_AUTHORITY_CANDIDATES = 500


class ResolutionOutcome(StrEnum):
    AUTO_LINK = "AUTO_LINK"
    REVIEW = "REVIEW"
    NO_MATCH = "NO_MATCH"
    CONFLICT = "CONFLICT"


class ResolutionBoundaryError(RuntimeError):
    """An authoritative source violated the tenant/T-Box security boundary."""


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    return normalized


def _confidence(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0.0 <= float(value) <= 1.0
    ):
        raise ValueError(f"{name} must be between zero and one")
    return float(value)


@dataclass(frozen=True, slots=True)
class AuthoritativeEvidence:
    """One authorized, published mention supporting a canonical target."""

    mention_revision_id: str
    document_id: str
    version_id: str
    chunk_id: str
    char_start: int
    char_end: int
    quoted_text: str

    def __post_init__(self) -> None:
        for name in (
            "mention_revision_id",
            "document_id",
            "version_id",
            "chunk_id",
        ):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        if (
            isinstance(self.char_start, bool)
            or not isinstance(self.char_start, int)
            or self.char_start < 0
            or isinstance(self.char_end, bool)
            or not isinstance(self.char_end, int)
            or self.char_end <= self.char_start
        ):
            raise ValueError("authoritative evidence range is invalid")
        if not isinstance(self.quoted_text, str) or not self.quoted_text:
            raise ValueError("authoritative evidence text must not be empty")
        if len(self.quoted_text) != self.char_end - self.char_start:
            raise ValueError("authoritative evidence text must match its range")


@dataclass(frozen=True, slots=True)
class AuthoritativeEntityProfile:
    """A canonical identity backed by authorized authoritative evidence."""

    entity: EntityIdentity
    ontology_version_id: str
    authority: AuthorityLevel
    status: GovernanceStatus
    evidence: tuple[AuthoritativeEvidence, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "ontology_version_id",
            _required_text(self.ontology_version_id, "ontology_version_id"),
        )
        if self.authority is not AuthorityLevel.AUTHORITATIVE:
            raise ValueError("resolution targets must be AUTHORITATIVE")
        if self.status is not GovernanceStatus.PUBLISHED:
            raise ValueError("resolution targets must be PUBLISHED")
        if not self.evidence:
            raise ValueError("authoritative target requires source evidence")
        revision_ids = [item.mention_revision_id for item in self.evidence]
        if len(revision_ids) != len(set(revision_ids)):
            raise ValueError("authoritative target evidence revisions must be unique")


@dataclass(frozen=True, slots=True)
class ExactAuthoritativeMatch:
    """Database-proven cardinality plus the target when it is globally unique."""

    match_count: int
    target: AuthoritativeEntityProfile | None = None
    matched_target_value: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.match_count, bool)
            or not isinstance(self.match_count, int)
            or self.match_count < 0
        ):
            raise ValueError("exact authoritative match_count must not be negative")
        if self.match_count == 1:
            if self.target is None:
                raise ValueError("one exact authoritative match requires its target")
            object.__setattr__(
                self,
                "matched_target_value",
                _required_text(self.matched_target_value, "matched_target_value"),
            )
        elif self.target is not None or self.matched_target_value is not None:
            raise ValueError(
                "zero or ambiguous exact authoritative matches cannot select a target"
            )


@dataclass(frozen=True, slots=True)
class ResolutionEvidence:
    """Why a candidate/target pair was proposed, with target provenance."""

    match_kind: str
    candidate_value: str
    target_value: str
    matcher_version: str
    authoritative_evidence: tuple[AuthoritativeEvidence, ...]

    def __post_init__(self) -> None:
        for name in (
            "match_kind",
            "candidate_value",
            "target_value",
            "matcher_version",
        ):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        if not self.authoritative_evidence:
            raise ValueError("match evidence requires authoritative source evidence")


@dataclass(frozen=True, slots=True)
class ResolutionSuggestion:
    """An immutable proposal for review; never an applied graph mutation."""

    candidate: EntityIdentity
    target: EntityIdentity | None
    ontology_version_id: str
    rule_version: str
    matcher_version: str
    evidence: tuple[ResolutionEvidence, ...]
    confidence: float
    outcome: ResolutionOutcome
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "ontology_version_id",
            _required_text(self.ontology_version_id, "ontology_version_id"),
        )
        object.__setattr__(
            self, "rule_version", _required_text(self.rule_version, "rule_version")
        )
        object.__setattr__(
            self,
            "matcher_version",
            _required_text(self.matcher_version, "matcher_version"),
        )
        object.__setattr__(self, "reason", _required_text(self.reason, "reason"))
        object.__setattr__(
            self,
            "confidence",
            _confidence(self.confidence, "resolution confidence"),
        )
        if not isinstance(self.outcome, ResolutionOutcome):
            raise TypeError("outcome must be a ResolutionOutcome")
        if self.target is None and self.evidence:
            raise ValueError("targetless resolution suggestions cannot carry match evidence")
        if self.target is not None:
            if self.target.tenant_id != self.candidate.tenant_id:
                raise ValueError("candidate and target must share one tenant")
            if self.target.entity_type != self.candidate.entity_type:
                raise ValueError("candidate and target must share one entity type")
            if not self.evidence:
                raise ValueError("targeted resolution suggestions require evidence")
        if self.outcome is ResolutionOutcome.AUTO_LINK and self.target is None:
            raise ValueError("AUTO_LINK requires one target")
        if self.outcome is ResolutionOutcome.NO_MATCH and self.target is not None:
            raise ValueError("NO_MATCH must not carry a target")


@dataclass(frozen=True, slots=True)
class ResolutionPolicy:
    rule_version: str = "authoritative-resolution-rules:v1"
    similarity_matcher_version: str = "sequence-matcher:v1"
    similarity_threshold: float = 0.84
    max_suggestions: int = 5
    authority_candidate_limit: int = 200

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "rule_version",
            _required_text(self.rule_version, "rule_version"),
        )
        object.__setattr__(
            self,
            "similarity_matcher_version",
            _required_text(
                self.similarity_matcher_version,
                "similarity_matcher_version",
            ),
        )
        _confidence(self.similarity_threshold, "similarity_threshold")
        for name, upper_bound in (
            ("max_suggestions", 50),
            ("authority_candidate_limit", MAX_AUTHORITY_CANDIDATES),
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= upper_bound
            ):
                raise ValueError(f"{name} must be between 1 and {upper_bound}")


class AuthoritativeEntitySource(Protocol):
    def find_exact_canonical_key(
        self,
        principal: Principal,
        *,
        ontology_version_id: str,
        entity_type: str,
        canonical_key: str,
    ) -> ExactAuthoritativeMatch: ...

    def find_exact_governed_alias(
        self,
        principal: Principal,
        *,
        ontology_version_id: str,
        entity_type: str,
        candidate_values: tuple[str, ...],
    ) -> ExactAuthoritativeMatch: ...

    def list_authoritative_entities(
        self,
        principal: Principal,
        *,
        ontology_version_id: str,
        entity_type: str,
        limit: int,
    ) -> tuple[AuthoritativeEntityProfile, ...]: ...


_ACTIVE_AUTHORITY_MATCH = """
MATCH (state:KnowledgePublicationState {tenant_id: $tenant_id})
      -[:ACTIVE_KNOWLEDGE_PUBLICATION]->
      (publication:KnowledgePublication {
          tenant_id: $tenant_id,
          status: 'ACTIVE'
      })
MATCH (publication)-[:PUBLISHES_KNOWLEDGE_REVISION]->
      (mention:GovernedEntityMentionRevision {
          tenant_id: $tenant_id,
          authority_level: 'AUTHORITATIVE',
          governance_status: 'PUBLISHED'
      })-[:REFERS_TO]->(entity:Entity {
          tenant_id: $tenant_id,
          entity_type: $entity_type
      })
MATCH (head:KnowledgeRecordHead {
          tenant_id: $tenant_id,
          record_kind: 'ENTITY_MENTION'
      })-[:CURRENT_REVISION]->(mention)
MATCH (mention)-[:IN_CHUNK]->(chunk:Chunk {tenant_id: $tenant_id})
MATCH (document:Document {tenant_id: $tenant_id})
      -[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot {
          tenant_id: $tenant_id,
          build_state: 'PUBLISHED'
      })-[:INCLUDES_CHUNK]->(chunk)
MATCH (document)-[:ACTIVE_VERSION]->(version:DocumentVersion {
    tenant_id: $tenant_id
})
MATCH (snapshot)-[:OF_VERSION]->(version)
MATCH (publication)-[:USES_KNOWLEDGE_SNAPSHOT]->(snapshot)
MATCH (:TBoxCatalog {tenant_id: $tenant_id})-[:ACTIVE_TBOX_VERSION]->
      (tbox:TBoxVersion {
          tenant_id: $tenant_id,
          tbox_id: $ontology_version_id,
          status: 'PUBLISHED'
      })-[:DECLARES_ENTITY_TYPE]->(declared:TBoxEntityType {
          name: $entity_type
      })
WHERE head.record_id = mention.record_id
  AND entity.entity_type = declared.name
  AND mention.entity_type = declared.name
  AND mention.entity_id = entity.entity_id
  AND mention.ontology_version_id = tbox.tbox_id
  AND mention.document_id = document.document_id
  AND mention.version_id = version.version_id
  AND mention.chunk_id = chunk.chunk_id
  AND mention.access_policy_id = chunk.access_policy_id
  AND mention.access_policy_version = chunk.access_policy_version
  AND mention.access_groups = chunk.access_groups
  AND chunk.char_start <= mention.evidence_char_start
  AND mention.evidence_char_start < mention.evidence_char_end
  AND mention.evidence_char_end <= chunk.char_end
  AND substring(
      chunk.text,
      mention.evidence_char_start - chunk.char_start,
      mention.evidence_char_end - mention.evidence_char_start
  ) = mention.evidence_text
  AND any(group IN $groups WHERE group IN mention.access_groups)
  AND any(group IN $groups WHERE group IN chunk.access_groups)
  AND any(group IN $groups WHERE group IN document.access_groups)
"""

_EVIDENCE_PROJECTION = """{
    mention_revision_id: mention.revision_id,
    document_id: document.document_id,
    version_id: version.version_id,
    chunk_id: chunk.chunk_id,
    char_start: mention.evidence_char_start,
    char_end: mention.evidence_char_end,
    quoted_text: mention.evidence_text
}"""


def _exact_authority_queries(predicate: str, matched_value: str) -> tuple[str, str]:
    """Build count/fetch statements guarded by one activation generation."""

    count_query = f"""
{_ACTIVE_AUTHORITY_MATCH}
  AND {predicate}
RETURN count(DISTINCT entity) AS match_count,
       min(entity.entity_id) AS only_entity_id,
       min(publication.publication_id) AS publication_id,
       min(state.activation_generation) AS activation_generation,
       count(DISTINCT publication) AS publication_count
"""
    target_query = f"""
{_ACTIVE_AUTHORITY_MATCH}
  AND state.activation_generation = $activation_generation
  AND publication.publication_id = $publication_id
  AND entity.entity_id = $only_entity_id
  AND {predicate}
WITH DISTINCT entity, tbox, mention, document, version, chunk,
     {matched_value} AS matched_value
ORDER BY mention.created_at DESC, mention.revision_id ASC
LIMIT 5
WITH entity, tbox, matched_value,
     collect({_EVIDENCE_PROJECTION}) AS evidence
RETURN {{
    entity_id: entity.entity_id,
    tenant_id: entity.tenant_id,
    entity_type: entity.entity_type,
    canonical_key: entity.canonical_key,
    canonical_name: entity.canonical_name,
    aliases: entity.aliases,
    ontology_version_id: tbox.tbox_id,
    matched_value: matched_value,
    evidence: evidence
}} AS target
"""
    return count_query, target_query

_ALIAS_PREDICATE = """any(
    alias IN coalesce(entity.aliases, [])
    WHERE any(
        pattern IN $alias_patterns
        WHERE normalize(alias, NFKC) =~ pattern
    )
)"""

_EXACT_CANONICAL_KEY_QUERIES = _exact_authority_queries(
    "entity.canonical_key = $canonical_key",
    "entity.canonical_key",
)

_EXACT_GOVERNED_ALIAS_QUERIES = _exact_authority_queries(
    _ALIAS_PREDICATE,
    "head([alias IN entity.aliases WHERE "
    "any(pattern IN $alias_patterns WHERE normalize(alias, NFKC) =~ pattern)])",
)

_AUTHORIZED_ENTITY_QUERY = f"""
{_ACTIVE_AUTHORITY_MATCH}
WITH DISTINCT entity, tbox, mention, document, version, chunk
ORDER BY entity.entity_id, mention.created_at DESC, mention.revision_id ASC
WITH entity, tbox, collect({_EVIDENCE_PROJECTION})[0..5] AS evidence
RETURN entity.entity_id AS entity_id,
       entity.tenant_id AS tenant_id,
       entity.entity_type AS entity_type,
       entity.canonical_key AS canonical_key,
       entity.canonical_name AS canonical_name,
       entity.aliases AS aliases,
       tbox.tbox_id AS ontology_version_id,
       evidence
ORDER BY entity.entity_id
LIMIT $limit
"""


def _governed_alias_pattern(value: str) -> str:
    """Return a literal, linear-time Java regex for the governed name key."""

    key = normalized_name_key(value)
    tokens = tuple(key.split())
    if not tokens:
        raise ValueError("candidate alias value must have a non-empty normalized key")
    separator = r"[^\p{L}\p{N}_]+"
    return r"(?iu)^\s*" + separator.join(re.escape(item) for item in tokens) + r"\s*$"


class Neo4jAuthoritativeEntitySource:
    """Read bounded, ACL-safe targets from the published authority layer."""

    def __init__(self, driver: object, database: str = "neo4j") -> None:
        if driver is None:
            raise ValueError("driver must not be None")
        self.driver = driver
        self.database = _required_text(database, "database")

    def find_exact_canonical_key(
        self,
        principal: Principal,
        *,
        ontology_version_id: str,
        entity_type: str,
        canonical_key: str,
    ) -> ExactAuthoritativeMatch:
        parameters = self._parameters(
            principal,
            ontology_version_id=ontology_version_id,
            entity_type=entity_type,
        )
        parameters["canonical_key"] = _required_text(canonical_key, "canonical_key")
        return self._exact_match(_EXACT_CANONICAL_KEY_QUERIES, parameters)

    def find_exact_governed_alias(
        self,
        principal: Principal,
        *,
        ontology_version_id: str,
        entity_type: str,
        candidate_values: tuple[str, ...],
    ) -> ExactAuthoritativeMatch:
        if not isinstance(candidate_values, tuple) or not candidate_values:
            raise ValueError("candidate_values must be a non-empty tuple")
        patterns = tuple(
            dict.fromkeys(
                _governed_alias_pattern(_required_text(value, "candidate alias value"))
                for value in candidate_values
            )
        )
        parameters = self._parameters(
            principal,
            ontology_version_id=ontology_version_id,
            entity_type=entity_type,
        )
        parameters["alias_patterns"] = list(patterns)
        return self._exact_match(_EXACT_GOVERNED_ALIAS_QUERIES, parameters)

    def list_authoritative_entities(
        self,
        principal: Principal,
        *,
        ontology_version_id: str,
        entity_type: str,
        limit: int,
    ) -> tuple[AuthoritativeEntityProfile, ...]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_AUTHORITY_CANDIDATES
        ):
            raise ValueError(
                f"limit must be between 1 and {MAX_AUTHORITY_CANDIDATES}"
            )
        parameters = self._parameters(
            principal,
            ontology_version_id=ontology_version_id,
            entity_type=entity_type,
        )
        parameters["limit"] = limit
        with self.driver.session(database=self.database) as session:  # type: ignore[attr-defined]
            rows = session.run(_AUTHORIZED_ENTITY_QUERY, **parameters)
            return tuple(self._profile(dict(row)) for row in rows)

    @staticmethod
    def _parameters(
        principal: Principal,
        *,
        ontology_version_id: str,
        entity_type: str,
    ) -> dict[str, Any]:
        return {
            "tenant_id": principal.tenant_id,
            "groups": sorted(principal.groups),
            "ontology_version_id": _required_text(
                ontology_version_id, "ontology_version_id"
            ),
            "entity_type": _required_text(entity_type, "entity_type"),
        }

    def _exact_match(
        self,
        queries: tuple[str, str],
        parameters: dict[str, Any],
    ) -> ExactAuthoritativeMatch:
        with self.driver.session(database=self.database) as session:  # type: ignore[attr-defined]
            return session.execute_read(
                self._exact_match_tx,
                queries,
                parameters,
            )

    @classmethod
    def _exact_match_tx(
        cls,
        tx: Any,
        queries: tuple[str, str],
        parameters: dict[str, Any],
    ) -> ExactAuthoritativeMatch:
        count_query, target_query = queries
        row = tx.run(count_query, **parameters).single()
        if row is None:
            raise ResolutionBoundaryError(
                "exact authoritative cardinality query returned no result"
            )
        match_count = row["match_count"]
        if (
            isinstance(match_count, bool)
            or not isinstance(match_count, int)
            or match_count < 0
        ):
            raise ResolutionBoundaryError(
                "exact authoritative cardinality query returned an invalid count"
            )
        if match_count != 1:
            return ExactAuthoritativeMatch(match_count)
        if (
            row["publication_count"] != 1
            or not row["publication_id"]
            or isinstance(row["activation_generation"], bool)
            or not isinstance(row["activation_generation"], int)
            or row["activation_generation"] < 1
            or not row["only_entity_id"]
        ):
            raise ResolutionBoundaryError(
                "unique exact authority result lacks one active publication boundary"
            )
        target_parameters = {
            **parameters,
            "publication_id": row["publication_id"],
            "activation_generation": row["activation_generation"],
            "only_entity_id": row["only_entity_id"],
        }
        target_row = tx.run(target_query, **target_parameters).single()
        if target_row is None or target_row["target"] is None:
            raise ResolutionBoundaryError(
                "active authority changed while resolving the unique exact target"
            )
        target_value = target_row["target"]
        target = dict(target_value)
        matched_value = target.pop("matched_value", None)
        return ExactAuthoritativeMatch(
            match_count=1,
            target=cls._profile(target),
            matched_target_value=matched_value,
        )

    @staticmethod
    def _profile(row: dict[str, Any]) -> AuthoritativeEntityProfile:
        entity = EntityIdentity(
            entity_id=row["entity_id"],
            tenant_id=row["tenant_id"],
            entity_type=row["entity_type"],
            canonical_key=row["canonical_key"],
            canonical_name=row["canonical_name"],
            aliases=tuple(row.get("aliases") or ()),
        )
        evidence = tuple(
            AuthoritativeEvidence(
                mention_revision_id=item["mention_revision_id"],
                document_id=item["document_id"],
                version_id=item["version_id"],
                chunk_id=item["chunk_id"],
                char_start=item["char_start"],
                char_end=item["char_end"],
                quoted_text=item["quoted_text"],
            )
            for item in row["evidence"]
        )
        return AuthoritativeEntityProfile(
            entity=entity,
            ontology_version_id=row["ontology_version_id"],
            authority=AuthorityLevel.AUTHORITATIVE,
            status=GovernanceStatus.PUBLISHED,
            evidence=evidence,
        )


class EntityResolutionService:
    """Resolve one candidate without ever applying a destructive merge."""

    def __init__(
        self,
        source: AuthoritativeEntitySource,
        *,
        active_tbox: TBoxVersion,
        policy: ResolutionPolicy | None = None,
    ) -> None:
        if active_tbox.status is not TBoxStatus.PUBLISHED:
            raise ValueError("entity resolution requires a published T-Box")
        self.source = source
        self.active_tbox = active_tbox
        self.policy = policy or ResolutionPolicy()
        self._entity_types = {item.name for item in active_tbox.entity_types}

    def suggest(
        self,
        principal: Principal,
        candidate: EntityIdentity,
    ) -> tuple[ResolutionSuggestion, ...]:
        boundary_reason: str | None = None
        if principal.tenant_id != self.active_tbox.tenant_id:
            boundary_reason = "principal tenant is outside the active T-Box boundary"
        elif candidate.tenant_id != principal.tenant_id:
            boundary_reason = "candidate and principal tenants do not match"
        elif candidate.entity_type not in self._entity_types:
            boundary_reason = "candidate type is outside the active T-Box"
        if boundary_reason is not None:
            return (
                self._targetless(
                    candidate,
                    ResolutionOutcome.CONFLICT,
                    boundary_reason,
                    matcher_version="boundary-check:v1",
                ),
            )

        exact_key = self.source.find_exact_canonical_key(
            principal,
            ontology_version_id=self.active_tbox.tbox_id,
            entity_type=candidate.entity_type,
            canonical_key=candidate.canonical_key,
        )
        self._validate_exact_match(candidate, exact_key)
        if exact_key.match_count > 1:
            return (
                self._targetless(
                    candidate,
                    ResolutionOutcome.CONFLICT,
                    "canonical key conflict exists in the active authority layer",
                    matcher_version="exact-canonical-key:v2",
                ),
            )
        if exact_key.target is not None:
            return (
                self._suggestion(
                    candidate,
                    exact_key.target,
                    outcome=ResolutionOutcome.AUTO_LINK,
                    reason="one globally unique authoritative canonical key matched",
                    match_kind="EXACT_CANONICAL_KEY",
                    candidate_value=candidate.canonical_key,
                    target_value=exact_key.matched_target_value or "",
                    matcher_version="exact-canonical-key:v2",
                    confidence=1.0,
                ),
            )

        candidate_names = self._name_values(candidate)
        alias_match = self.source.find_exact_governed_alias(
            principal,
            ontology_version_id=self.active_tbox.tbox_id,
            entity_type=candidate.entity_type,
            candidate_values=tuple(value for value, _key in candidate_names),
        )
        self._validate_exact_match(candidate, alias_match)
        if alias_match.match_count > 1:
            return (
                self._targetless(
                    candidate,
                    ResolutionOutcome.CONFLICT,
                    "the governed alias resolves to multiple active authoritative entities",
                    matcher_version="normalized-governed-alias:v2",
                ),
            )
        if alias_match.target is not None:
            target_alias = alias_match.matched_target_value or ""
            candidate_value = self._matched_candidate_value(
                candidate_names,
                target_alias,
            )
            return (
                self._suggestion(
                    candidate,
                    alias_match.target,
                    outcome=ResolutionOutcome.AUTO_LINK,
                    reason="one globally unique authoritative alias matched",
                    match_kind="EXACT_GOVERNED_ALIAS",
                    candidate_value=candidate_value,
                    target_value=target_alias,
                    matcher_version="normalized-governed-alias:v2",
                    confidence=0.995,
                ),
            )

        targets = self.source.list_authoritative_entities(
            principal,
            ontology_version_id=self.active_tbox.tbox_id,
            entity_type=candidate.entity_type,
            limit=self.policy.authority_candidate_limit,
        )
        self._validate_targets(candidate, targets)

        exact_names: list[tuple[AuthoritativeEntityProfile, str, str]] = []
        for target in targets:
            target_key = normalized_name_key(target.entity.canonical_name)
            for candidate_value, candidate_key in candidate_names:
                if candidate_key and candidate_key == target_key:
                    exact_names.append(
                        (target, candidate_value, target.entity.canonical_name)
                    )
                    break
        if exact_names:
            outcome = (
                ResolutionOutcome.CONFLICT
                if len({item[0].entity.entity_id for item in exact_names}) > 1
                else ResolutionOutcome.REVIEW
            )
            return tuple(
                self._suggestion(
                    candidate,
                    target,
                    outcome=outcome,
                    reason=(
                        "same-name authoritative homonyms require conflict review"
                        if outcome is ResolutionOutcome.CONFLICT
                        else "a name match alone is insufficient for automatic identity merge"
                    ),
                    match_kind="EXACT_CANONICAL_NAME",
                    candidate_value=candidate_value,
                    target_value=target_name,
                    matcher_version="normalized-canonical-name:v1",
                    confidence=0.98,
                )
                for target, candidate_value, target_name in exact_names[
                    : self.policy.max_suggestions
                ]
            )

        similar: list[
            tuple[float, AuthoritativeEntityProfile, str, str]
        ] = []
        for target in targets:
            target_values = (
                (target.entity.canonical_name, normalized_name_key(target.entity.canonical_name)),
                *(
                    (alias, normalized_name_key(alias))
                    for alias in target.entity.aliases
                ),
            )
            best: tuple[float, str, str] | None = None
            for candidate_value, candidate_key in candidate_names:
                for target_value, target_key in target_values:
                    score = SequenceMatcher(None, candidate_key, target_key).ratio()
                    if best is None or score > best[0]:
                        best = (score, candidate_value, target_value)
            if best is not None and best[0] >= self.policy.similarity_threshold:
                similar.append((best[0], target, best[1], best[2]))
        similar.sort(key=lambda item: (-item[0], item[1].entity.entity_id))
        if similar:
            return tuple(
                self._suggestion(
                    candidate,
                    target,
                    outcome=ResolutionOutcome.REVIEW,
                    reason="similarity is a review suggestion and never an automatic merge",
                    match_kind="SIMILAR_NAME",
                    candidate_value=candidate_value,
                    target_value=target_value,
                    matcher_version=self.policy.similarity_matcher_version,
                    confidence=score,
                )
                for score, target, candidate_value, target_value in similar[
                    : self.policy.max_suggestions
                ]
            )

        return (
            self._targetless(
                candidate,
                ResolutionOutcome.NO_MATCH,
                "no authorized authoritative target met a deterministic or review threshold",
                matcher_version=self.policy.similarity_matcher_version,
            ),
        )

    def _validate_exact_match(
        self,
        candidate: EntityIdentity,
        result: ExactAuthoritativeMatch,
    ) -> None:
        if not isinstance(result, ExactAuthoritativeMatch):
            raise ResolutionBoundaryError(
                "authoritative source returned an invalid exact-match result"
            )
        if result.target is not None:
            self._validate_targets(candidate, (result.target,))

    @staticmethod
    def _matched_candidate_value(
        candidate_names: tuple[tuple[str, str], ...],
        target_alias: str,
    ) -> str:
        target_key = normalized_name_key(target_alias)
        for candidate_value, candidate_key in candidate_names:
            if candidate_key == target_key:
                return candidate_value
        raise ResolutionBoundaryError(
            "exact alias query returned a target value outside the candidate aliases"
        )

    def _validate_targets(
        self,
        candidate: EntityIdentity,
        targets: tuple[AuthoritativeEntityProfile, ...],
    ) -> None:
        entity_ids: set[str] = set()
        for target in targets:
            if (
                target.entity.tenant_id != candidate.tenant_id
                or target.entity.entity_type != candidate.entity_type
                or target.ontology_version_id != self.active_tbox.tbox_id
                or target.authority is not AuthorityLevel.AUTHORITATIVE
                or target.status is not GovernanceStatus.PUBLISHED
            ):
                raise ResolutionBoundaryError(
                    "authoritative source returned data outside the requested boundary"
                )
            if target.entity.entity_id in entity_ids:
                raise ResolutionBoundaryError(
                    "authoritative source returned duplicate canonical entities"
                )
            entity_ids.add(target.entity.entity_id)

    @staticmethod
    def _name_values(entity: EntityIdentity) -> tuple[tuple[str, str], ...]:
        values = (entity.canonical_name, *entity.aliases)
        result: list[tuple[str, str]] = []
        seen: set[str] = set()
        for value in values:
            key = normalized_name_key(value)
            if key and key not in seen:
                result.append((value, key))
                seen.add(key)
        return tuple(result)

    def _deterministic_result(
        self,
        candidate: EntityIdentity,
        targets: tuple[AuthoritativeEntityProfile, ...],
        *,
        match_kind: str,
        candidate_value: str,
        target_value: Any,
        matcher_version: str,
        confidence: float,
    ) -> tuple[ResolutionSuggestion, ...]:
        outcome = (
            ResolutionOutcome.AUTO_LINK
            if len(targets) == 1
            else ResolutionOutcome.CONFLICT
        )
        return tuple(
            self._suggestion(
                candidate,
                target,
                outcome=outcome,
                reason=(
                    "one unique authoritative canonical key matched"
                    if outcome is ResolutionOutcome.AUTO_LINK
                    else "canonical key conflict exists in the authority layer"
                ),
                match_kind=match_kind,
                candidate_value=candidate_value,
                target_value=target_value(target),
                matcher_version=matcher_version,
                confidence=confidence,
            )
            for target in targets[: self.policy.max_suggestions]
        )

    def _suggestion(
        self,
        candidate: EntityIdentity,
        target: AuthoritativeEntityProfile,
        *,
        outcome: ResolutionOutcome,
        reason: str,
        match_kind: str,
        candidate_value: str,
        target_value: str,
        matcher_version: str,
        confidence: float,
    ) -> ResolutionSuggestion:
        evidence = ResolutionEvidence(
            match_kind=match_kind,
            candidate_value=candidate_value,
            target_value=target_value,
            matcher_version=matcher_version,
            authoritative_evidence=target.evidence,
        )
        return ResolutionSuggestion(
            candidate=candidate,
            target=target.entity,
            ontology_version_id=self.active_tbox.tbox_id,
            rule_version=self.policy.rule_version,
            matcher_version=matcher_version,
            evidence=(evidence,),
            confidence=confidence,
            outcome=outcome,
            reason=reason,
        )

    def _targetless(
        self,
        candidate: EntityIdentity,
        outcome: ResolutionOutcome,
        reason: str,
        *,
        matcher_version: str,
    ) -> ResolutionSuggestion:
        return ResolutionSuggestion(
            candidate=candidate,
            target=None,
            ontology_version_id=self.active_tbox.tbox_id,
            rule_version=self.policy.rule_version,
            matcher_version=matcher_version,
            evidence=(),
            confidence=0.0,
            outcome=outcome,
            reason=reason,
        )
