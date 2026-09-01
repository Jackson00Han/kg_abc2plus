"""Tenant-scoped graph quality audits and human governance decisions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from .governance import GraphGovernancePolicy, normalized_name_key
from .resolution import ResolutionCandidate, ResolutionDecision, resolve_entity_pair


class QueryDriver(Protocol):
    def execute_query(self, query_: str, **kwargs: object) -> tuple[Any, Any, Any]: ...


class IssueSeverity(StrEnum):
    ERROR = "ERROR"
    WARNING = "WARNING"
    REVIEW = "REVIEW"


@dataclass(frozen=True, slots=True)
class GraphQualityIssue:
    issue_id: str
    code: str
    severity: IssueSeverity
    object_kind: str
    object_id: str
    detail: str


@dataclass(frozen=True, slots=True)
class HumanReviewSampleItem:
    object_kind: str
    object_id: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GraphQualityReport:
    run_id: str
    tenant_id: str
    policy_id: str
    policy_version: int
    corpus_revision: int
    generated_at: datetime
    counts: tuple[tuple[str, int], ...]
    issues: tuple[GraphQualityIssue, ...]
    review_sample: tuple[HumanReviewSampleItem, ...]

    @property
    def passed(self) -> bool:
        return not any(issue.severity is IssueSeverity.ERROR for issue in self.issues)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["generated_at"] = self.generated_at.isoformat()
        value["counts"] = dict(self.counts)
        value["passed"] = self.passed
        return value

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _issue(
    run_seed: str,
    code: str,
    severity: IssueSeverity,
    object_kind: str,
    object_id: str,
    detail: str,
) -> GraphQualityIssue:
    issue_id = f"quality-issue:{_stable_hash([run_seed, code, severity.value, object_kind, object_id, detail])}"
    return GraphQualityIssue(issue_id, code, severity, object_kind, object_id, detail)


class Neo4jGraphQualityService:
    """Audit only the tenant's published snapshots; never return source text."""

    def __init__(self, driver: QueryDriver, database: str = "neo4j") -> None:
        self.driver = driver
        self.database = database

    def audit(
        self,
        tenant_id: str,
        policy: GraphGovernancePolicy,
        *,
        generated_at: datetime,
        sample_seed: str = "graph-review-v1",
        sample_size: int = 20,
    ) -> GraphQualityReport:
        if not tenant_id.strip() or not sample_seed.strip():
            raise ValueError("tenant_id and sample_seed must not be empty")
        if generated_at.tzinfo is None or generated_at.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        if sample_size <= 0:
            raise ValueError("sample_size must be positive")

        entities = self._active_entities(tenant_id)
        mentions = self._active_mentions(tenant_id)
        assertions = self._active_assertions(tenant_id)
        orphan_ids = self._orphan_entities(tenant_id)
        corpus_revision = self._corpus_revision(tenant_id)
        run_seed = _stable_hash(
            [tenant_id, policy.policy_id, policy.policy_version, corpus_revision, sample_seed]
        )
        issues: list[GraphQualityIssue] = []

        mentions_by_entity: dict[str, list[dict[str, Any]]] = {}
        for mention in mentions:
            mentions_by_entity.setdefault(str(mention["entity_id"]), []).append(mention)

        accepted_assertions = [item for item in assertions if bool(item["accepted"])]
        governed_assertions = assertions
        degree: dict[str, int] = {}
        for assertion in accepted_assertions:
            for key in ("subject_entity_id", "object_entity_id"):
                identifier = assertion.get(key)
                if identifier:
                    degree[str(identifier)] = degree.get(str(identifier), 0) + 1

        names: dict[tuple[str, str], list[str]] = {}
        aliases: dict[tuple[str, str], list[str]] = {}
        for entity in entities:
            entity_id = str(entity["entity_id"])
            entity_type = str(entity["entity_type"])
            canonical_name = str(entity.get("canonical_name") or "")
            if canonical_name:
                names.setdefault((entity_type, normalized_name_key(canonical_name)), []).append(entity_id)
            for alias in entity.get("aliases") or ():
                aliases.setdefault((entity_type, normalized_name_key(str(alias))), []).append(entity_id)
            profiles = entity.get("active_profiles") or ()
            profile_keys = {
                (
                    normalized_name_key(str(profile.get("canonical_name") or canonical_name)),
                    tuple(
                        sorted(
                            normalized_name_key(str(alias))
                            for alias in (profile.get("aliases") or ())
                        )
                    ),
                )
                for profile in profiles
            }
            if len(profile_keys) > 1:
                issues.append(
                    _issue(
                        run_seed,
                        "ENTITY_PROFILE_CONFLICT",
                        IssueSeverity.REVIEW,
                        "Entity",
                        entity_id,
                        "active snapshots disagree on the canonical name or alias set",
                    )
                )
            rule = policy.entity_rules_by_type.get(entity_type)
            namespace = str(entity.get("canonical_key") or "").partition(":")[0].casefold()
            if rule is None or namespace not in rule.canonical_key_namespaces:
                issues.append(
                    _issue(run_seed, "INVALID_ENTITY_SCHEMA", IssueSeverity.ERROR, "Entity", entity_id,
                           "active entity type or canonical-key namespace is outside policy")
                )
            if not mentions_by_entity.get(entity_id):
                issues.append(
                    _issue(run_seed, "ENTITY_WITHOUT_ACTIVE_MENTION", IssueSeverity.ERROR, "Entity", entity_id,
                           "active entity has no mention in its active snapshot")
                )
            if degree.get(entity_id, 0) == 0:
                issues.append(
                    _issue(run_seed, "ISOLATED_ENTITY", IssueSeverity.WARNING, "Entity", entity_id,
                           "entity participates in no accepted active assertion")
                )
            if degree.get(entity_id, 0) > policy.anomalous_hub_degree:
                issues.append(
                    _issue(run_seed, "ANOMALOUS_HUB", IssueSeverity.REVIEW, "Entity", entity_id,
                           f"accepted degree {degree[entity_id]} exceeds policy threshold {policy.anomalous_hub_degree}")
                )

        for (_, key), identifiers in sorted(names.items()):
            unique = sorted(set(identifiers))
            if key and len(unique) > 1:
                issues.append(
                    _issue(run_seed, "POTENTIAL_DUPLICATE_NAME", IssueSeverity.REVIEW, "EntityPair", "|".join(unique),
                           "normalized canonical name collides; authoritative evidence is required before merge")
                )
        for (_, key), identifiers in sorted(aliases.items()):
            unique = sorted(set(identifiers))
            if key and len(unique) > 1:
                issues.append(
                    _issue(run_seed, "ALIAS_COLLISION", IssueSeverity.REVIEW, "EntityPair", "|".join(unique),
                           "normalized alias is shared by distinct entities and is not auto-merged")
                )

        entities_by_id = {str(item["entity_id"]): item for item in entities}
        for assertion in governed_assertions:
            assertion_id = str(assertion["assertion_id"])
            violation_severity = (
                IssueSeverity.ERROR if bool(assertion["accepted"]) else IssueSeverity.WARNING
            )
            subject_id = str(assertion.get("subject_entity_id") or "")
            object_id = assertion.get("object_entity_id")
            object_kind = str(assertion["object_kind"])
            subject_type = (
                str(entities_by_id[subject_id]["entity_type"])
                if subject_id in entities_by_id
                else None
            )
            object_type = (
                str(entities_by_id[str(object_id)]["entity_type"])
                if object_id is not None and str(object_id) in entities_by_id
                else None
            )
            if not policy.allows_relationship(
                str(assertion["predicate"]), subject_type, object_kind, object_type
            ):
                issues.append(
                    _issue(run_seed, "INVALID_RELATIONSHIP_PATTERN", violation_severity, "Assertion", assertion_id,
                           "accepted predicate and endpoint types are outside policy")
                )
            if int(assertion["evidence_links"]) != 1 or not bool(assertion["evidence_is_active"]):
                issues.append(
                    _issue(run_seed, "UNSUPPORTED_ASSERTION", violation_severity, "Assertion", assertion_id,
                           "accepted assertion lacks exactly one active evidence chunk")
                )
                continue
            evidence_start = int(assertion["evidence_char_start"])
            evidence_end = int(assertion["evidence_char_end"])
            chunk_start = int(assertion["chunk_char_start"])
            chunk_text = str(assertion["chunk_text"])
            relative_start = evidence_start - chunk_start
            relative_end = evidence_end - chunk_start
            range_valid = 0 <= relative_start < relative_end <= len(chunk_text)
            literal = assertion.get("literal_value")
            literal_supported = (
                object_kind != "literal"
                or (literal is not None and range_valid and str(literal) in chunk_text[relative_start:relative_end])
            )
            endpoint_ids = {subject_id}
            if object_id is not None:
                endpoint_ids.add(str(object_id))
            endpoint_support = all(
                any(
                    str(mention["chunk_id"]) == str(assertion["evidence_chunk_id"])
                    and int(mention["char_start"]) >= evidence_start
                    and int(mention["char_end"]) <= evidence_end
                    for mention in mentions_by_entity.get(entity_id, ())
                )
                for entity_id in endpoint_ids
            )
            if not range_valid or not literal_supported or not endpoint_support:
                issues.append(
                    _issue(run_seed, "UNSUPPORTED_ASSERTION", violation_severity, "Assertion", assertion_id,
                           "accepted assertion range, literal, or endpoint mention is unsupported by source")
                )

        for entity_id in orphan_ids:
            issues.append(
                _issue(run_seed, "ORPHAN_ENTITY", IssueSeverity.ERROR, "Entity", entity_id,
                       "entity has no mention or assertion provenance anywhere in the tenant")
            )

        sample_candidates = [
            HumanReviewSampleItem(
                "Entity",
                str(entity["entity_id"]),
                tuple(sorted(str(item["mention_id"]) for item in mentions_by_entity.get(str(entity["entity_id"]), ()))),
            )
            for entity in entities
        ] + [
            HumanReviewSampleItem(
                "Assertion",
                str(assertion["assertion_id"]),
                (str(assertion["evidence_chunk_id"]),) if assertion.get("evidence_chunk_id") else (),
            )
            for assertion in accepted_assertions
        ]
        sample = tuple(
            sorted(
                sample_candidates,
                key=lambda item: _stable_hash([sample_seed, item.object_kind, item.object_id]),
            )[:sample_size]
        )
        issues_tuple = tuple(
            sorted(issues, key=lambda item: (item.severity.value, item.code, item.object_id))
        )
        counts = tuple(
            sorted(
                {
                    "active_entities": len(entities),
                    "active_mentions": len(mentions),
                    "active_assertions": len(assertions),
                    "accepted_assertions": len(accepted_assertions),
                    "quarantined_assertions": len(assertions) - len(accepted_assertions),
                    "issues": len(issues_tuple),
                    "errors": sum(item.severity is IssueSeverity.ERROR for item in issues_tuple),
                    "warnings": sum(item.severity is IssueSeverity.WARNING for item in issues_tuple),
                    "review_items": sum(item.severity is IssueSeverity.REVIEW for item in issues_tuple),
                }.items()
            )
        )
        report_material = {
            "tenant_id": tenant_id,
            "policy_id": policy.policy_id,
            "policy_version": policy.policy_version,
            "corpus_revision": corpus_revision,
            "generated_at": generated_at.isoformat(),
            "counts": counts,
            "issues": [asdict(item) for item in issues_tuple],
            "sample": [asdict(item) for item in sample],
        }
        run_id = f"quality-run:{_stable_hash(report_material)}"
        report = GraphQualityReport(
            run_id,
            tenant_id,
            policy.policy_id,
            policy.policy_version,
            corpus_revision,
            generated_at,
            counts,
            issues_tuple,
            sample,
        )
        self._persist_report(report, policy)
        return report

    def adjudicate(
        self,
        *,
        tenant_id: str,
        target_kind: str,
        target_id: str,
        action: str,
        reviewer_id: str,
        rationale: str,
        decided_at: datetime,
        policy: GraphGovernancePolicy,
    ) -> str:
        if target_kind not in {"Entity", "Assertion"}:
            raise ValueError("target_kind must be Entity or Assertion")
        if action not in {"ACCEPT", "QUARANTINE"}:
            raise ValueError("action must be ACCEPT or QUARANTINE")
        if decided_at.tzinfo is None or decided_at.utcoffset() is None:
            raise ValueError("decided_at must be timezone-aware")
        for value, name in ((tenant_id, "tenant_id"), (target_id, "target_id"),
                            (reviewer_id, "reviewer_id"), (rationale, "rationale")):
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        if action == "ACCEPT":
            report = self.audit(
                tenant_id,
                policy,
                generated_at=decided_at,
                sample_seed=f"pre-accept:{target_kind}:{target_id}",
                sample_size=1,
            )
            if any(
                issue.object_id == target_id
                and issue.code in {"INVALID_RELATIONSHIP_PATTERN", "UNSUPPORTED_ASSERTION", "INVALID_ENTITY_SCHEMA", "ENTITY_WITHOUT_ACTIVE_MENTION"}
                for issue in report.issues
            ):
                raise ValueError("cannot accept a target with unresolved quality errors")
        decision_material = [
            tenant_id, target_kind, target_id, action, reviewer_id, rationale,
            decided_at.isoformat(), policy.policy_id, policy.policy_version,
        ]
        decision_id = f"review-decision:{_stable_hash(decision_material)}"
        label = target_kind
        records, _, _ = self.driver.execute_query(
            f"""
            MATCH (target:{label} {{{label.lower()}_id: $target_id, tenant_id: $tenant_id}})
            MERGE (decision:GraphReviewDecision {{decision_id: $decision_id}})
            ON CREATE SET decision.tenant_id = $tenant_id,
                          decision.target_kind = $target_kind,
                          decision.target_id = $target_id,
                          decision.action = $action,
                          decision.reviewer_id = $reviewer_id,
                          decision.rationale = $rationale,
                          decision.decided_at = $decided_at,
                          decision.policy_id = $policy_id,
                          decision.policy_version = $policy_version
            MERGE (decision)-[:REVIEWS]->(target)
            SET target.governance_status = $status,
                target.governance_policy_id = $policy_id,
                target.governance_reviewed_at = $decided_at
            RETURN decision.decision_id AS decision_id,
                   decision.action = $action
                       AND decision.reviewer_id = $reviewer_id
                       AND decision.rationale = $rationale AS compatible
            """,
            parameters_={
                "target_id": target_id,
                "tenant_id": tenant_id,
                "decision_id": decision_id,
                "target_kind": target_kind,
                "action": action,
                "status": (
                    "ACCEPTED_BY_REVIEW" if action == "ACCEPT" else "QUARANTINED"
                ),
                "reviewer_id": reviewer_id,
                "rationale": rationale,
                "decided_at": decided_at,
                "policy_id": policy.policy_id,
                "policy_version": policy.policy_version,
            },
            database_=self.database,
        )
        if len(records) != 1 or not records[0]["compatible"]:
            raise ValueError("governance target is missing or decision conflicts")
        return decision_id

    def resolve_and_record(
        self,
        left: ResolutionCandidate,
        right: ResolutionCandidate,
        *,
        policy: GraphGovernancePolicy,
        decided_at: datetime,
    ) -> ResolutionDecision:
        """Apply the fixed resolver and persist its exact mention evidence."""
        if left.tenant_id != right.tenant_id:
            raise ValueError("cross-tenant resolution decisions cannot be persisted")
        if decided_at.tzinfo is None or decided_at.utcoffset() is None:
            raise ValueError("decided_at must be timezone-aware")
        decision = resolve_entity_pair(left, right)
        record_id = f"resolution-record:{_stable_hash([decision.decision_id, policy.policy_id, policy.policy_version])}"
        evidence_ids = list(decision.evidence_mention_ids)
        records, _, _ = self.driver.execute_query(
            """
            MATCH (policy:GraphGovernancePolicy {policy_id: $policy_id})
            UNWIND $evidence_ids AS mention_id
            MATCH (mention:EntityMention {
                mention_id: mention_id,
                tenant_id: $tenant_id
            })
            WITH policy, collect(mention) AS mentions
            WHERE size(mentions) = size($evidence_ids)
            MERGE (decision:EntityResolutionDecision {decision_id: $decision_id})
            ON CREATE SET decision.tenant_id = $tenant_id,
                          decision.raw_decision_id = $raw_decision_id,
                          decision.left_candidate_id = $left_candidate_id,
                          decision.right_candidate_id = $right_candidate_id,
                          decision.outcome = $outcome,
                          decision.rule_id = $rule_id,
                          decision.rationale = $rationale,
                          decision.policy_id = $policy_id,
                          decision.policy_version = $policy_version,
                          decision.decided_at = $decided_at
            MERGE (decision)-[:USES_POLICY]->(policy)
            WITH decision, mentions,
                 decision.raw_decision_id = $raw_decision_id
                 AND decision.outcome = $outcome
                 AND decision.rule_id = $rule_id AS compatible
            FOREACH (mention IN mentions |
                MERGE (decision)-[:EVIDENCE_MENTION]->(mention)
            )
            RETURN decision.decision_id AS decision_id, compatible,
                   size(mentions) AS evidence_count
            """,
            parameters_={
                "decision_id": record_id,
                "raw_decision_id": decision.decision_id,
                "tenant_id": left.tenant_id,
                "left_candidate_id": decision.left_candidate_id,
                "right_candidate_id": decision.right_candidate_id,
                "outcome": decision.outcome.value,
                "rule_id": decision.rule_id,
                "rationale": decision.rationale,
                "policy_id": policy.policy_id,
                "policy_version": policy.policy_version,
                "decided_at": decided_at,
                "evidence_ids": evidence_ids,
            },
            database_=self.database,
        )
        if (
            len(records) != 1
            or not bool(records[0]["compatible"])
            or int(records[0]["evidence_count"]) != len(evidence_ids)
        ):
            raise ValueError("resolution evidence is missing or the decision conflicts")
        return decision

    def _query(self, query: str, tenant_id: str) -> list[dict[str, Any]]:
        records, _, _ = self.driver.execute_query(
            query,
            parameters_={"tenant_id": tenant_id},
            database_=self.database,
        )
        return [dict(record) for record in records]

    def _active_entities(self, tenant_id: str) -> list[dict[str, Any]]:
        return self._query(
            """
            MATCH (:Document {tenant_id: $tenant_id})-[:ACTIVE_SNAPSHOT]->
                  (snapshot:KnowledgeSnapshot {tenant_id: $tenant_id, build_state: 'PUBLISHED'})
                  -[membership:INCLUDES_ENTITY]->(entity:Entity {tenant_id: $tenant_id})
            WITH entity, collect(DISTINCT {
                canonical_name: membership.canonical_name,
                aliases: coalesce(membership.aliases, [])
            }) AS active_profiles
            RETURN entity.entity_id AS entity_id,
                   entity.entity_type AS entity_type,
                   entity.canonical_key AS canonical_key,
                   entity.canonical_name AS canonical_name,
                   coalesce(entity.aliases, []) AS aliases,
                   active_profiles
            ORDER BY entity_id
            """,
            tenant_id,
        )

    def _active_mentions(self, tenant_id: str) -> list[dict[str, Any]]:
        return self._query(
            """
            MATCH (:Document {tenant_id: $tenant_id})-[:ACTIVE_SNAPSHOT]->
                  (snapshot:KnowledgeSnapshot {tenant_id: $tenant_id, build_state: 'PUBLISHED'})
                  -[:INCLUDES_MENTION]->(mention:EntityMention {tenant_id: $tenant_id})
            RETURN DISTINCT mention.mention_id AS mention_id,
                   mention.entity_id AS entity_id,
                   mention.chunk_id AS chunk_id,
                   mention.char_start AS char_start,
                   mention.char_end AS char_end
            ORDER BY mention_id
            """,
            tenant_id,
        )

    def _active_assertions(self, tenant_id: str) -> list[dict[str, Any]]:
        return self._query(
            """
            MATCH (:Document {tenant_id: $tenant_id})-[:ACTIVE_SNAPSHOT]->
                  (snapshot:KnowledgeSnapshot {tenant_id: $tenant_id, build_state: 'PUBLISHED'})
                  -[membership:INCLUDES_ASSERTION]->(assertion:Assertion {tenant_id: $tenant_id})
            OPTIONAL MATCH (assertion)-[:SUBJECT]->(subject:Entity {tenant_id: $tenant_id})
            OPTIONAL MATCH (assertion)-[:OBJECT]->(object:Entity {tenant_id: $tenant_id})
            OPTIONAL MATCH (assertion)-[:EVIDENCED_BY]->(chunk:Chunk {tenant_id: $tenant_id})
            WITH snapshot, membership, assertion, subject, object,
                 collect(DISTINCT chunk) AS evidence_chunks
            WITH snapshot, membership, assertion, subject, object, evidence_chunks,
                 CASE WHEN size(evidence_chunks) = 1 THEN evidence_chunks[0] ELSE null END AS chunk
            RETURN assertion.assertion_id AS assertion_id,
                   assertion.predicate AS predicate,
                   assertion.object_kind AS object_kind,
                   assertion.literal_value AS literal_value,
                   assertion.evidence_char_start AS evidence_char_start,
                   assertion.evidence_char_end AS evidence_char_end,
                   subject.entity_id AS subject_entity_id,
                   object.entity_id AS object_entity_id,
                   size(evidence_chunks) AS evidence_links,
                   chunk.chunk_id AS evidence_chunk_id,
                   chunk.char_start AS chunk_char_start,
                   chunk.text AS chunk_text,
                   chunk IS NOT NULL AND EXISTS {
                       MATCH (snapshot)-[:INCLUDES_CHUNK]->(chunk)
                   } AS evidence_is_active,
                   (
                       membership.accepted = true
                       OR assertion.governance_status = 'ACCEPTED_BY_REVIEW'
                   )
                       AND coalesce(assertion.governance_status, 'ACCEPTED') IN
                           ['ACCEPTED', 'ACCEPTED_BY_REVIEW']
                       AND coalesce(subject.governance_status, 'ACCEPTED') IN
                           ['ACCEPTED', 'ACCEPTED_BY_REVIEW']
                       AND (assertion.object_kind <> 'entity'
                            OR coalesce(object.governance_status, 'ACCEPTED') IN
                               ['ACCEPTED', 'ACCEPTED_BY_REVIEW'])
                       AS accepted
            ORDER BY assertion_id
            """,
            tenant_id,
        )

    def _orphan_entities(self, tenant_id: str) -> tuple[str, ...]:
        rows = self._query(
            """
            MATCH (entity:Entity {tenant_id: $tenant_id})
            WHERE NOT EXISTS { MATCH (:EntityMention)-[:REFERS_TO]->(entity) }
              AND NOT EXISTS { MATCH (:Assertion)-[:SUBJECT|OBJECT]->(entity) }
            RETURN entity.entity_id AS entity_id ORDER BY entity_id
            """,
            tenant_id,
        )
        return tuple(str(row["entity_id"]) for row in rows)

    def _corpus_revision(self, tenant_id: str) -> int:
        rows = self._query(
            """
            OPTIONAL MATCH (state:TenantCorpusState {tenant_id: $tenant_id})
            RETURN coalesce(state.corpus_revision, 0) AS corpus_revision
            """,
            tenant_id,
        )
        return int(rows[0]["corpus_revision"])

    def _persist_report(
        self,
        report: GraphQualityReport,
        policy: GraphGovernancePolicy,
    ) -> None:
        policy_hash = policy.payload_hash
        report_payload = report.to_json()
        report_hash = hashlib.sha256(report_payload.encode("utf-8")).hexdigest()
        issues = [
            {
                **asdict(issue),
                "severity": issue.severity.value,
            }
            for issue in report.issues
        ]
        records, _, _ = self.driver.execute_query(
            """
            MERGE (policy:GraphGovernancePolicy {policy_id: $policy_id})
            ON CREATE SET policy.policy_version = $policy_version,
                          policy.payload_hash = $policy_hash,
                          policy.payload = $policy_payload
            WITH policy,
                 policy.policy_version = $policy_version
                 AND policy.payload_hash = $policy_hash AS policy_compatible
            MERGE (run:GraphQualityRun {run_id: $run_id})
            ON CREATE SET run.tenant_id = $tenant_id,
                          run.policy_id = $policy_id,
                          run.corpus_revision = $corpus_revision,
                          run.generated_at = $generated_at,
                          run.report_hash = $report_hash,
                          run.report = $report,
                          run.passed = $passed
            MERGE (run)-[:USES_POLICY]->(policy)
            WITH run, policy_compatible,
                 run.report_hash = $report_hash AS report_compatible
            UNWIND CASE WHEN size($issues) = 0 THEN [null] ELSE $issues END AS item
            FOREACH (_ IN CASE WHEN item IS NULL THEN [] ELSE [1] END |
                MERGE (issue:GraphQualityIssue {issue_id: item.issue_id})
                ON CREATE SET issue += item
                MERGE (run)-[:HAS_ISSUE]->(issue)
            )
            RETURN policy_compatible, report_compatible
            """,
            parameters_={
                "policy_id": policy.policy_id,
                "policy_version": policy.policy_version,
                "policy_hash": policy_hash,
                "policy_payload": policy.canonical_payload,
                "run_id": report.run_id,
                "tenant_id": report.tenant_id,
                "corpus_revision": report.corpus_revision,
                "generated_at": report.generated_at,
                "report_hash": report_hash,
                "report": report_payload,
                "passed": report.passed,
                "issues": issues,
            },
            database_=self.database,
        )
        if not records or not all(
            bool(record["policy_compatible"]) and bool(record["report_compatible"])
            for record in records
        ):
            raise ValueError("governance policy or report identity conflicts")
