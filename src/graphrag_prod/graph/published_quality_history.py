"""Immutable evidence history for active-publication graph quality audits.

The live quality auditor deliberately performs no writes.  This module composes
that read boundary with a second, bounded transaction that records the exact
report as audit evidence.  A recorded run never changes publication state or
promotes graph material; a failing quality report is evidence, not trust.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from neo4j import unit_of_work

from graphrag_prod.domain.access import Principal

from .published_quality import (
    Neo4jPublishedGraphQualityService,
    PUBLISHED_QUALITY_RULESET_VERSION,
    PublishedGraphQualityAuthorizationError,
    PublishedGraphQualityError,
    PublishedGraphQualityIssue,
    PublishedGraphQualityReport,
    PublishedGraphReviewSampleItem,
)
from .quality import IssueSeverity

PUBLISHED_QUALITY_HISTORY_CAPABILITY = "knowledge:quality"
PUBLISHED_QUALITY_HISTORY_SCHEMA_VERSION = "published-quality-history-v1"

_MAX_LIST_LIMIT = 50
_MAX_TRANSACTION_TIMEOUT_SECONDS = 300.0
_MAX_COUNT_ROWS = 128
_MAX_ISSUE_RECORDS = 5_000
_MAX_REVIEW_SAMPLE_RECORDS = 200
_MAX_ACL_REQUIREMENTS = 10_000
_RUN_PREFIX = "published-graph-quality:"
_ISSUE_PREFIX = "published-quality-issue:"
_SAMPLE_PREFIX = "published-quality-sample:"
_ACL_PREFIX = "published-quality-acl:"
_SHA256_LENGTH = 64


class SessionDriver(Protocol):
    def session(self, **kwargs: object) -> Any: ...


class PublishedGraphQualityAuditor(Protocol):
    def audit(self, principal: Principal) -> PublishedGraphQualityReport: ...


class PublishedGraphQualityHistoryError(RuntimeError):
    """Base error with a backend-detail-free public message."""

    code = "PUBLISHED_GRAPH_QUALITY_HISTORY_ERROR"

    def __init__(self, message: str = "quality audit history operation failed") -> None:
        super().__init__(message)


class PublishedGraphQualityHistoryConflict(PublishedGraphQualityHistoryError):
    code = "PUBLISHED_GRAPH_QUALITY_HISTORY_CONFLICT"

    def __init__(self) -> None:
        super().__init__("quality audit history is unavailable or conflicted")


class PublishedGraphQualityHistoryUnavailable(PublishedGraphQualityHistoryError):
    code = "PUBLISHED_GRAPH_QUALITY_HISTORY_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("quality audit history is temporarily unavailable")


@dataclass(frozen=True, slots=True)
class PublishedGraphQualityRun:
    """One immutable recorded audit and its first-writer attribution."""

    report: PublishedGraphQualityReport
    recorded_by: str
    recorded_at: datetime
    record_hash: str

    @property
    def run_id(self) -> str:
        return self.report.run_id

    @property
    def tenant_id(self) -> str:
        return self.report.tenant_id

    @property
    def passed(self) -> bool:
        return self.report.passed

    def to_dict(self) -> dict[str, Any]:
        return {
            "report": self.report.to_dict(),
            "recorded_by": self.recorded_by,
            "recorded_at": self.recorded_at.isoformat(),
            "record_hash": self.record_hash,
        }


@dataclass(frozen=True, slots=True)
class PublishedGraphQualityRunSummary:
    run_id: str
    tenant_id: str
    publication_id: str
    publication_generation: int
    ontology_version_id: str
    corpus_revision: int
    graph_digest: str
    ruleset_version: str
    passed: bool
    total_issue_count: int
    total_error_count: int
    issues_truncated: bool
    counts: tuple[tuple[str, int], ...]
    recorded_by: str
    recorded_at: datetime
    record_hash: str


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _stable_hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _SHA256_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _required_text(value: object, name: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be empty")
    normalized = value.strip()
    if len(normalized) > maximum or any(c in normalized for c in "\x00\r\n"):
        raise ValueError(f"{name} is invalid")
    return normalized


def _aware_utc(value: object, name: str) -> datetime:
    if hasattr(value, "to_native"):
        value = value.to_native()
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PublishedGraphQualityHistoryConflict() from exc
    if not isinstance(value, datetime):
        raise PublishedGraphQualityHistoryConflict()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _positive_limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("limit must be an integer")
    if not 1 <= value <= _MAX_LIST_LIMIT:
        raise ValueError(f"limit must be between 1 and {_MAX_LIST_LIMIT}")
    return value


def _require_quality_capability(principal: Principal) -> None:
    if not isinstance(principal, Principal):
        raise TypeError("principal must be a Principal")
    if PUBLISHED_QUALITY_HISTORY_CAPABILITY not in principal.capabilities:
        raise PublishedGraphQualityAuthorizationError()


def _report_document(report: PublishedGraphQualityReport) -> dict[str, Any]:
    if not isinstance(report, PublishedGraphQualityReport):
        raise TypeError("quality auditor returned an invalid report")
    document = {
        "run_id": report.run_id,
        "ruleset_version": report.ruleset_version,
        "tenant_id": report.tenant_id,
        "publication_id": report.publication_id,
        "publication_generation": report.publication_generation,
        "manifest_hash": report.manifest_hash,
        "ontology_version_id": report.ontology_version_id,
        "tbox_checksum": report.tbox_checksum,
        "corpus_revision": report.corpus_revision,
        "graph_digest": report.graph_digest,
        "counts": [[name, count] for name, count in report.counts],
        "total_issue_count": report.total_issue_count,
        "total_error_count": report.total_error_count,
        "issues_truncated": report.issues_truncated,
        "passed": report.passed,
        "issues": [
            {
                "issue_id": item.issue_id,
                "code": item.code,
                "severity": item.severity.value,
                "object_kind": item.object_kind,
                "object_id": item.object_id,
                "detail": item.detail,
            }
            for item in report.issues
        ],
        "review_sample": [
            {
                "object_kind": item.object_kind,
                "object_id": item.object_id,
                "issue_codes": list(item.issue_codes),
                "evidence_chunk_ids": list(item.evidence_chunk_ids),
            }
            for item in report.review_sample
        ],
    }
    # Decode our own representation once.  This validates types, ordering,
    # digests, and derived pass state before any write is attempted.
    if _report_from_document(document) != report:
        raise ValueError("quality auditor returned a non-canonical report")
    return document


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise PublishedGraphQualityHistoryConflict()
    return value


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise PublishedGraphQualityHistoryConflict()
    result = tuple(_required_text(item, "stored identifier") for item in value)
    if len(result) != len(set(result)):
        raise PublishedGraphQualityHistoryConflict()
    return result


def _report_from_document(value: object) -> PublishedGraphQualityReport:
    if not isinstance(value, Mapping):
        raise PublishedGraphQualityHistoryConflict()
    document = dict(value)
    expected_keys = {
        "run_id",
        "ruleset_version",
        "tenant_id",
        "publication_id",
        "publication_generation",
        "manifest_hash",
        "ontology_version_id",
        "tbox_checksum",
        "corpus_revision",
        "graph_digest",
        "counts",
        "total_issue_count",
        "total_error_count",
        "issues_truncated",
        "passed",
        "issues",
        "review_sample",
    }
    if set(document) != expected_keys:
        raise PublishedGraphQualityHistoryConflict()
    run_id = _required_text(document["run_id"], "run_id")
    if not run_id.startswith(_RUN_PREFIX) or not _is_digest(
        run_id.removeprefix(_RUN_PREFIX)
    ):
        raise PublishedGraphQualityHistoryConflict()
    for name in ("manifest_hash", "tbox_checksum", "graph_digest"):
        if not _is_digest(document[name]):
            raise PublishedGraphQualityHistoryConflict()
    raw_counts = document["counts"]
    if isinstance(raw_counts, (str, bytes)) or not isinstance(raw_counts, Sequence):
        raise PublishedGraphQualityHistoryConflict()
    if len(raw_counts) > _MAX_COUNT_ROWS:
        raise PublishedGraphQualityHistoryConflict()
    counts: list[tuple[str, int]] = []
    for item in raw_counts:
        if isinstance(item, (str, bytes)) or not isinstance(item, Sequence):
            raise PublishedGraphQualityHistoryConflict()
        if len(item) != 2:
            raise PublishedGraphQualityHistoryConflict()
        counts.append(
            (
                _required_text(item[0], "count name"),
                _integer(item[1], "count"),
            )
        )
    if counts != sorted(counts) or len(counts) != len({name for name, _ in counts}):
        raise PublishedGraphQualityHistoryConflict()
    raw_issues = document["issues"]
    if isinstance(raw_issues, (str, bytes)) or not isinstance(raw_issues, Sequence):
        raise PublishedGraphQualityHistoryConflict()
    if len(raw_issues) > _MAX_ISSUE_RECORDS:
        raise PublishedGraphQualityHistoryConflict()
    issues: list[PublishedGraphQualityIssue] = []
    for raw in raw_issues:
        if not isinstance(raw, Mapping) or set(raw) != {
            "issue_id",
            "code",
            "severity",
            "object_kind",
            "object_id",
            "detail",
        }:
            raise PublishedGraphQualityHistoryConflict()
        try:
            severity = IssueSeverity(raw["severity"])
        except (TypeError, ValueError) as exc:
            raise PublishedGraphQualityHistoryConflict() from exc
        issue_id = _required_text(raw["issue_id"], "issue_id")
        if not issue_id.startswith(_ISSUE_PREFIX) or not _is_digest(
            issue_id.removeprefix(_ISSUE_PREFIX)
        ):
            raise PublishedGraphQualityHistoryConflict()
        issues.append(
            PublishedGraphQualityIssue(
                issue_id,
                _required_text(raw["code"], "issue code"),
                severity,
                _required_text(raw["object_kind"], "object kind"),
                _required_text(raw["object_id"], "object id"),
                _required_text(raw["detail"], "issue detail", maximum=4_096),
            )
        )
    if len({item.issue_id for item in issues}) != len(issues):
        raise PublishedGraphQualityHistoryConflict()
    raw_samples = document["review_sample"]
    if isinstance(raw_samples, (str, bytes)) or not isinstance(raw_samples, Sequence):
        raise PublishedGraphQualityHistoryConflict()
    if len(raw_samples) > _MAX_REVIEW_SAMPLE_RECORDS:
        raise PublishedGraphQualityHistoryConflict()
    samples: list[PublishedGraphReviewSampleItem] = []
    for raw in raw_samples:
        if not isinstance(raw, Mapping) or set(raw) != {
            "object_kind",
            "object_id",
            "issue_codes",
            "evidence_chunk_ids",
        }:
            raise PublishedGraphQualityHistoryConflict()
        samples.append(
            PublishedGraphReviewSampleItem(
                _required_text(raw["object_kind"], "sample object kind"),
                _required_text(raw["object_id"], "sample object id"),
                _string_tuple(raw["issue_codes"]),
                _string_tuple(raw["evidence_chunk_ids"]),
            )
        )
    total_issue_count = _integer(document["total_issue_count"], "issue count")
    total_error_count = _integer(document["total_error_count"], "error count")
    if total_issue_count < len(issues) or total_error_count > total_issue_count:
        raise PublishedGraphQualityHistoryConflict()
    if not isinstance(document["issues_truncated"], bool) or not isinstance(
        document["passed"], bool
    ):
        raise PublishedGraphQualityHistoryConflict()
    if document["issues_truncated"] != (total_issue_count > len(issues)):
        raise PublishedGraphQualityHistoryConflict()
    if document["passed"] != (total_error_count == 0):
        raise PublishedGraphQualityHistoryConflict()
    ruleset_version = _required_text(
        document["ruleset_version"], "ruleset_version"
    )
    if ruleset_version != PUBLISHED_QUALITY_RULESET_VERSION:
        raise PublishedGraphQualityHistoryConflict()
    return PublishedGraphQualityReport(
        run_id=run_id,
        ruleset_version=ruleset_version,
        tenant_id=_required_text(document["tenant_id"], "tenant_id"),
        publication_id=_required_text(
            document["publication_id"], "publication_id"
        ),
        publication_generation=_integer(
            document["publication_generation"],
            "publication_generation",
            minimum=1,
        ),
        manifest_hash=str(document["manifest_hash"]),
        ontology_version_id=_required_text(
            document["ontology_version_id"], "ontology_version_id"
        ),
        tbox_checksum=str(document["tbox_checksum"]),
        corpus_revision=_integer(document["corpus_revision"], "corpus_revision"),
        graph_digest=str(document["graph_digest"]),
        counts=tuple(counts),
        total_issue_count=total_issue_count,
        total_error_count=total_error_count,
        issues_truncated=bool(document["issues_truncated"]),
        issues=tuple(issues),
        review_sample=tuple(samples),
    )


def _report_from_json(value: object) -> PublishedGraphQualityReport:
    if not isinstance(value, str):
        raise PublishedGraphQualityHistoryConflict()
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise PublishedGraphQualityHistoryConflict() from exc
    try:
        report = _report_from_document(decoded)
        if _canonical_json(_report_document(report)) != value:
            raise PublishedGraphQualityHistoryConflict()
        return report
    except PublishedGraphQualityHistoryConflict:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise PublishedGraphQualityHistoryConflict() from exc


def _issue_payloads(report: PublishedGraphQualityReport) -> tuple[dict[str, Any], ...]:
    values: list[dict[str, Any]] = []
    for ordinal, issue in enumerate(report.issues):
        payload = {
            "issue_id": issue.issue_id,
            "code": issue.code,
            "severity": issue.severity.value,
            "object_kind": issue.object_kind,
            "object_id": issue.object_id,
            "detail": issue.detail,
        }
        values.append(
            {
                **payload,
                "tenant_id": report.tenant_id,
                "run_id": report.run_id,
                "payload_hash": _stable_hash(payload),
                "ordinal": ordinal,
            }
        )
    return tuple(values)


def _sample_payloads(report: PublishedGraphQualityReport) -> tuple[dict[str, Any], ...]:
    values: list[dict[str, Any]] = []
    for ordinal, sample in enumerate(report.review_sample):
        payload = {
            "object_kind": sample.object_kind,
            "object_id": sample.object_id,
            "issue_codes": list(sample.issue_codes),
            "evidence_chunk_ids": list(sample.evidence_chunk_ids),
        }
        sample_id = _SAMPLE_PREFIX + _stable_hash(
            [report.run_id, ordinal, payload]
        )
        values.append(
            {
                "sample_id": sample_id,
                **payload,
                "tenant_id": report.tenant_id,
                "run_id": report.run_id,
                "payload_hash": _stable_hash(payload),
                "ordinal": ordinal,
            }
        )
    return tuple(values)


def _normalize_acl_requirements(
    raw_values: Sequence[object],
) -> tuple[tuple[str, ...], ...]:
    values: set[tuple[str, ...]] = set()
    for raw in raw_values:
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
            raise PublishedGraphQualityHistoryConflict()
        groups = tuple(
            sorted(
                {
                    _required_text(group, "access group")
                    for group in raw
                }
            )
        )
        if not groups:
            raise PublishedGraphQualityHistoryConflict()
        values.add(groups)
    if not values or len(values) > _MAX_ACL_REQUIREMENTS:
        raise PublishedGraphQualityHistoryConflict()
    return tuple(sorted(values))


def _acl_payloads(
    report: PublishedGraphQualityReport,
    requirements: Sequence[tuple[str, ...]],
) -> tuple[dict[str, Any], ...]:
    values: list[dict[str, Any]] = []
    for ordinal, groups in enumerate(requirements):
        payload = {"access_groups": list(groups)}
        values.append(
            {
                "requirement_id": _ACL_PREFIX
                + _stable_hash([report.run_id, payload]),
                **payload,
                "tenant_id": report.tenant_id,
                "run_id": report.run_id,
                "payload_hash": _stable_hash(payload),
                "ordinal": ordinal,
            }
        )
    return tuple(values)


def _manifest(items: Sequence[Mapping[str, Any]], id_key: str) -> str:
    return _canonical_json(
        [
            {
                "id": item[id_key],
                "ordinal": item["ordinal"],
                "payload_hash": item["payload_hash"],
            }
            for item in items
        ]
    )


_BOUNDARY_QUERY = """
// published-quality-history:boundary
MATCH (state:KnowledgePublicationState {tenant_id: $tenant_id})
      -[active:ACTIVE_KNOWLEDGE_PUBLICATION]->
      (publication:KnowledgePublication {
          tenant_id: $tenant_id,
          publication_id: $publication_id,
          generation: $publication_generation,
          manifest_hash: $manifest_hash,
          ontology_version_id: $ontology_version_id,
          status: 'ACTIVE'
      })
MATCH (publication)-[binding:USES_TBOX_VERSION]->(tbox:TBoxVersion {
    tenant_id: $tenant_id,
    tbox_id: $ontology_version_id,
    checksum: $tbox_checksum
})
MATCH (corpus:TenantCorpusState {
    tenant_id: $tenant_id,
    corpus_revision: $corpus_revision
})
WITH state, publication, tbox, corpus, count(active) AS active_count,
     count(binding) AS binding_count
WHERE active_count = 1
  AND binding_count = 1
  AND COUNT {
      MATCH (state)-[:ACTIVE_KNOWLEDGE_PUBLICATION]->(:KnowledgePublication)
  } = 1
  AND COUNT {
      MATCH (publication)-[:USES_TBOX_VERSION]->(:TBoxVersion)
  } = 1
  AND corpus.corpus_revision = $corpus_revision
CALL (publication) {
    OPTIONAL MATCH (publication)-[:PUBLISHES_KNOWLEDGE_REVISION]->(revision)
    RETURN [value IN collect(revision.access_groups) WHERE value IS NOT NULL]
           AS revision_groups
}
CALL (publication) {
    OPTIONAL MATCH (publication)-[:PUBLISHES_KNOWLEDGE_REVISION]->(revision)
          -[:IN_CHUNK|EVIDENCED_BY]->(chunk:Chunk)
    RETURN [value IN collect(chunk.access_groups) WHERE value IS NOT NULL]
           AS evidence_chunk_groups
}
CALL (publication) {
    OPTIONAL MATCH (publication)-[:PUBLISHES_KNOWLEDGE_REVISION]->(revision)
    OPTIONAL MATCH (document:Document {document_id: revision.document_id})
    RETURN [value IN collect(document.access_groups) WHERE value IS NOT NULL]
           AS document_groups
}
CALL (publication) {
    OPTIONAL MATCH (publication)-[:USES_KNOWLEDGE_SNAPSHOT]->
          (snapshot:KnowledgeSnapshot)-[:INCLUDES_CHUNK]->(chunk:Chunk)
    RETURN [value IN collect(chunk.access_groups) WHERE value IS NOT NULL]
           AS snapshot_chunk_groups
}
CALL (publication) {
    OPTIONAL MATCH (publication)-[:USES_KNOWLEDGE_SNAPSHOT]->
          (snapshot:KnowledgeSnapshot)
    OPTIONAL MATCH (document:Document)-[:ACTIVE_SNAPSHOT]->(snapshot)
    RETURN [value IN collect(document.access_groups) WHERE value IS NOT NULL]
           AS snapshot_document_groups
}
WITH publication, tbox,
     revision_groups + evidence_chunk_groups + document_groups
       + snapshot_chunk_groups + snapshot_document_groups AS acl_requirements,
     NOT EXISTS {
         MATCH (publication)-[:PUBLISHES_KNOWLEDGE_REVISION]->(revision)
         WHERE revision.tenant_id <> $tenant_id
            OR none(group IN $groups
                    WHERE group IN coalesce(revision.access_groups, []))
     }
     AND NOT EXISTS {
         MATCH (publication)-[:PUBLISHES_KNOWLEDGE_REVISION]->(revision)
               -[:IN_CHUNK|EVIDENCED_BY]->(chunk:Chunk)
         WHERE chunk.tenant_id <> $tenant_id
            OR none(group IN $groups
                    WHERE group IN coalesce(chunk.access_groups, []))
     }
     AND NOT EXISTS {
         MATCH (publication)-[:PUBLISHES_KNOWLEDGE_REVISION]->(revision)
         MATCH (document:Document {document_id: revision.document_id})
         WHERE document.tenant_id <> $tenant_id
            OR none(group IN $groups
                    WHERE group IN coalesce(document.access_groups, []))
     }
     AND NOT EXISTS {
         MATCH (publication)-[:USES_KNOWLEDGE_SNAPSHOT]->
               (:KnowledgeSnapshot)-[:INCLUDES_CHUNK]->(chunk:Chunk)
         WHERE chunk.tenant_id <> $tenant_id
            OR none(group IN $groups
                    WHERE group IN coalesce(chunk.access_groups, []))
     }
     AND NOT EXISTS {
         MATCH (publication)-[:USES_KNOWLEDGE_SNAPSHOT]->
               (snapshot:KnowledgeSnapshot)
         MATCH (document:Document)-[:ACTIVE_SNAPSHOT]->(snapshot)
         WHERE document.tenant_id <> $tenant_id
            OR snapshot.tenant_id <> $tenant_id
            OR none(group IN $groups
                    WHERE group IN coalesce(document.access_groups, []))
     } AS acl_complete
RETURN publication.publication_id AS publication_id,
       publication.generation AS publication_generation,
       publication.manifest_hash AS manifest_hash,
       publication.ontology_version_id AS ontology_version_id,
       tbox.tbox_id AS tbox_id,
       tbox.checksum AS tbox_checksum,
       acl_requirements,
       acl_complete
"""


_LOCK_BOUNDARY_QUERY = """
// published-quality-history:lock-boundary
MATCH (publication_state:KnowledgePublicationState {tenant_id: $tenant_id})
MATCH (corpus_state:TenantCorpusState {tenant_id: $tenant_id})
SET publication_state.__publication_cas_lock = randomUUID(),
    corpus_state.__published_quality_history_cas_lock = randomUUID()
WITH publication_state, corpus_state
REMOVE publication_state.__publication_cas_lock,
       corpus_state.__published_quality_history_cas_lock
RETURN publication_state.tenant_id AS tenant_id,
       corpus_state.corpus_revision AS corpus_revision
"""


_CREATE_RUN_QUERY = """
// published-quality-history:create-run
MATCH (publication:KnowledgePublication {
    tenant_id: $tenant_id,
    publication_id: $publication_id,
    generation: $publication_generation,
    manifest_hash: $manifest_hash,
    ontology_version_id: $ontology_version_id
})-[:USES_TBOX_VERSION]->(tbox:TBoxVersion {
    tenant_id: $tenant_id,
    tbox_id: $ontology_version_id,
    checksum: $tbox_checksum
})
CREATE (run:PublishedGraphQualityRun)
SET run = $properties
CREATE (run)-[:AUDITS_KNOWLEDGE_PUBLICATION {
    publication_generation: $publication_generation,
    manifest_hash: $manifest_hash
}]->(publication)
CREATE (run)-[:USES_AUDITED_TBOX_VERSION {
    tbox_checksum: $tbox_checksum
}]->(tbox)
RETURN run.run_id AS run_id
"""


_CREATE_ISSUES_QUERY = """
MATCH (run:PublishedGraphQualityRun {run_id: $run_id})
UNWIND $items AS item
CREATE (issue:PublishedGraphQualityIssue)
SET issue = item.properties
CREATE (run)-[:HAS_PUBLISHED_QUALITY_ISSUE {ordinal: item.ordinal}]->(issue)
RETURN count(issue) AS count
"""


_CREATE_SAMPLES_QUERY = """
MATCH (run:PublishedGraphQualityRun {run_id: $run_id})
UNWIND $items AS item
CREATE (sample:PublishedGraphQualityReviewSample)
SET sample = item.properties
CREATE (run)-[:HAS_PUBLISHED_QUALITY_SAMPLE {ordinal: item.ordinal}]->(sample)
RETURN count(sample) AS count
"""


_CREATE_ACL_QUERY = """
MATCH (run:PublishedGraphQualityRun {run_id: $run_id})
UNWIND $items AS item
CREATE (requirement:PublishedGraphQualityAclRequirement)
SET requirement = item.properties
CREATE (run)-[:REQUIRES_PUBLISHED_QUALITY_ACCESS {ordinal: item.ordinal}]
      ->(requirement)
RETURN count(requirement) AS count
"""


_AUTHORIZATION_QUERY = """
// published-quality-history:authorization
MATCH (run:PublishedGraphQualityRun {
    tenant_id: $tenant_id,
    run_id: $run_id
})
CALL (run) {
    OPTIONAL MATCH (run)-[edge:REQUIRES_PUBLISHED_QUALITY_ACCESS]->
          (requirement:PublishedGraphQualityAclRequirement)
    WITH edge, requirement ORDER BY edge.ordinal
    RETURN [item IN collect(
        CASE WHEN requirement IS NULL THEN null ELSE {
            node: properties(requirement),
            edge: properties(edge),
            relationship_count: COUNT { MATCH (requirement)-[]-() }
        } END
    ) WHERE item IS NOT NULL] AS requirements
}
RETURN run.tenant_id AS tenant_id,
       run.run_id AS run_id,
       run.acl_manifest_json AS acl_manifest_json,
       run.acl_requirement_count AS acl_requirement_count,
       run.authorization_hash AS authorization_hash,
       requirements
"""


_LOAD_QUERY = """
// published-quality-history:load
MATCH (run:PublishedGraphQualityRun {
    tenant_id: $tenant_id,
    run_id: $run_id
})
CALL (run) {
    OPTIONAL MATCH (run)-[edge:AUDITS_KNOWLEDGE_PUBLICATION]->
          (publication:KnowledgePublication)
    RETURN [item IN collect(
        CASE WHEN publication IS NULL THEN null ELSE {
            node: properties(publication), edge: properties(edge)
        } END
    ) WHERE item IS NOT NULL] AS publications
}
CALL (run) {
    OPTIONAL MATCH (run)-[edge:USES_AUDITED_TBOX_VERSION]->(tbox:TBoxVersion)
    RETURN [item IN collect(
        CASE WHEN tbox IS NULL THEN null ELSE {
            node: properties(tbox), edge: properties(edge)
        } END
    ) WHERE item IS NOT NULL] AS tboxes
}
CALL (run) {
    OPTIONAL MATCH (run)-[edge:HAS_PUBLISHED_QUALITY_ISSUE]->
          (issue:PublishedGraphQualityIssue)
    WITH edge, issue ORDER BY edge.ordinal
    RETURN [item IN collect(
        CASE WHEN issue IS NULL THEN null ELSE {
            node: properties(issue), edge: properties(edge),
            relationship_count: COUNT { MATCH (issue)-[]-() }
        } END
    ) WHERE item IS NOT NULL] AS issues
}
CALL (run) {
    OPTIONAL MATCH (run)-[edge:HAS_PUBLISHED_QUALITY_SAMPLE]->
          (sample:PublishedGraphQualityReviewSample)
    WITH edge, sample ORDER BY edge.ordinal
    RETURN [item IN collect(
        CASE WHEN sample IS NULL THEN null ELSE {
            node: properties(sample), edge: properties(edge),
            relationship_count: COUNT { MATCH (sample)-[]-() }
        } END
    ) WHERE item IS NOT NULL] AS samples
}
CALL (run) {
    OPTIONAL MATCH (run)-[edge:REQUIRES_PUBLISHED_QUALITY_ACCESS]->
          (requirement:PublishedGraphQualityAclRequirement)
    WITH edge, requirement ORDER BY edge.ordinal
    RETURN [item IN collect(
        CASE WHEN requirement IS NULL THEN null ELSE {
            node: properties(requirement), edge: properties(edge),
            relationship_count: COUNT { MATCH (requirement)-[]-() }
        } END
    ) WHERE item IS NOT NULL] AS requirements
}
CALL (run) {
    OPTIONAL MATCH (run)-[edge]->()
    RETURN [value IN collect(type(edge)) WHERE value IS NOT NULL]
           AS outgoing_types
}
CALL (run) {
    OPTIONAL MATCH ()-[edge]->(run)
    RETURN [value IN collect(type(edge)) WHERE value IS NOT NULL]
           AS incoming_types
}
RETURN properties(run) AS run, publications, tboxes, issues, samples,
       requirements, outgoing_types, incoming_types
"""


_LIST_QUERY = """
// published-quality-history:list
MATCH (run:PublishedGraphQualityRun {tenant_id: $tenant_id})
WHERE ($publication_id IS NULL OR run.publication_id = $publication_id)
  AND run.acl_requirement_count > 0
  AND COUNT {
      MATCH (run)-[:REQUIRES_PUBLISHED_QUALITY_ACCESS]->
            (:PublishedGraphQualityAclRequirement)
  } = run.acl_requirement_count
  AND NOT EXISTS {
      MATCH (run)-[:REQUIRES_PUBLISHED_QUALITY_ACCESS]->
            (requirement:PublishedGraphQualityAclRequirement)
      WHERE none(group IN $groups WHERE group IN requirement.access_groups)
  }
RETURN run.run_id AS run_id
ORDER BY run.publication_generation DESC,
         run.recorded_at DESC,
         run.publication_id ASC,
         run.run_id ASC
LIMIT $limit
"""


_RUN_PROPERTY_KEYS = {
    "run_id",
    "tenant_id",
    "record_schema_version",
    "record_kind",
    "ruleset_version",
    "publication_id",
    "publication_generation",
    "manifest_hash",
    "ontology_version_id",
    "tbox_checksum",
    "corpus_revision",
    "graph_digest",
    "counts_json",
    "total_issue_count",
    "total_error_count",
    "issues_truncated",
    "passed",
    "report_json",
    "report_hash",
    "issue_manifest_json",
    "sample_manifest_json",
    "acl_manifest_json",
    "issue_record_count",
    "sample_record_count",
    "acl_requirement_count",
    "recorded_by",
    "recorded_at",
    "authorization_hash",
    "integrity_hash",
}


def _edge(value: object, expected: Mapping[str, Any]) -> None:
    if not isinstance(value, Mapping) or dict(value) != dict(expected):
        raise PublishedGraphQualityHistoryConflict()


def _child_rows(
    raw_rows: object,
    expected: Sequence[Mapping[str, Any]],
    *,
    id_key: str,
) -> None:
    if isinstance(raw_rows, (str, bytes)) or not isinstance(raw_rows, Sequence):
        raise PublishedGraphQualityHistoryConflict()
    if len(raw_rows) != len(expected):
        raise PublishedGraphQualityHistoryConflict()
    for raw, target in zip(raw_rows, expected, strict=True):
        if not isinstance(raw, Mapping):
            raise PublishedGraphQualityHistoryConflict()
        node = raw.get("node")
        if not isinstance(node, Mapping):
            raise PublishedGraphQualityHistoryConflict()
        stored = dict(node)
        target_properties = {key: value for key, value in target.items() if key != "ordinal"}
        if stored != target_properties or stored.get(id_key) != target[id_key]:
            raise PublishedGraphQualityHistoryConflict()
        _edge(raw.get("edge"), {"ordinal": target["ordinal"]})
        if raw.get("relationship_count") != 1:
            raise PublishedGraphQualityHistoryConflict()


def _authorize_row(
    row: Mapping[str, Any],
    principal: Principal,
) -> tuple[tuple[str, ...], ...]:
    if row.get("tenant_id") != principal.tenant_id:
        raise PublishedGraphQualityHistoryConflict()
    raw = row.get("requirements")
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise PublishedGraphQualityHistoryConflict()
    payloads: list[dict[str, Any]] = []
    requirements: list[tuple[str, ...]] = []
    for ordinal, item in enumerate(raw):
        if not isinstance(item, Mapping) or not isinstance(item.get("node"), Mapping):
            raise PublishedGraphQualityHistoryConflict()
        node = dict(item["node"])
        expected_keys = {
            "requirement_id",
            "tenant_id",
            "run_id",
            "access_groups",
            "payload_hash",
        }
        if set(node) != expected_keys:
            raise PublishedGraphQualityHistoryConflict()
        groups = _string_tuple(node["access_groups"])
        if tuple(sorted(groups)) != groups:
            raise PublishedGraphQualityHistoryConflict()
        payload = {"access_groups": list(groups)}
        if node["payload_hash"] != _stable_hash(payload):
            raise PublishedGraphQualityHistoryConflict()
        expected_id = _ACL_PREFIX + _stable_hash([row["run_id"], payload])
        if (
            node["requirement_id"] != expected_id
            or node["tenant_id"] != principal.tenant_id
            or node["run_id"] != row["run_id"]
            or item.get("relationship_count") != 1
        ):
            raise PublishedGraphQualityHistoryConflict()
        _edge(item.get("edge"), {"ordinal": ordinal})
        requirements.append(groups)
        payloads.append({**node, "ordinal": ordinal})
    if row.get("acl_requirement_count") != len(payloads) or not payloads:
        raise PublishedGraphQualityHistoryConflict()
    acl_manifest = _manifest(payloads, "requirement_id")
    if row.get("acl_manifest_json") != acl_manifest:
        raise PublishedGraphQualityHistoryConflict()
    authorization_hash = _stable_hash(
        {
            "tenant_id": principal.tenant_id,
            "run_id": row["run_id"],
            "acl_manifest_json": acl_manifest,
        }
    )
    if row.get("authorization_hash") != authorization_hash:
        raise PublishedGraphQualityHistoryConflict()
    if any(not (set(groups) & principal.groups) for groups in requirements):
        raise PublishedGraphQualityAuthorizationError()
    return tuple(requirements)


def _run_from_row(
    row: Mapping[str, Any],
    principal: Principal,
) -> PublishedGraphQualityRun:
    raw_properties = row.get("run")
    if not isinstance(raw_properties, Mapping):
        raise PublishedGraphQualityHistoryConflict()
    properties = dict(raw_properties)
    if set(properties) != _RUN_PROPERTY_KEYS:
        raise PublishedGraphQualityHistoryConflict()
    report = _report_from_json(properties["report_json"])
    if report.tenant_id != principal.tenant_id:
        raise PublishedGraphQualityHistoryConflict()
    report_document = _report_document(report)
    report_hash = _stable_hash(report_document)
    if properties["report_hash"] != report_hash:
        raise PublishedGraphQualityHistoryConflict()
    duplicate_values = {
        "run_id": report.run_id,
        "tenant_id": report.tenant_id,
        "record_schema_version": PUBLISHED_QUALITY_HISTORY_SCHEMA_VERSION,
        "record_kind": "AUDIT_EVIDENCE",
        "ruleset_version": report.ruleset_version,
        "publication_id": report.publication_id,
        "publication_generation": report.publication_generation,
        "manifest_hash": report.manifest_hash,
        "ontology_version_id": report.ontology_version_id,
        "tbox_checksum": report.tbox_checksum,
        "corpus_revision": report.corpus_revision,
        "graph_digest": report.graph_digest,
        "counts_json": _canonical_json(report_document["counts"]),
        "total_issue_count": report.total_issue_count,
        "total_error_count": report.total_error_count,
        "issues_truncated": report.issues_truncated,
        "passed": report.passed,
    }
    if any(properties.get(key) != value for key, value in duplicate_values.items()):
        raise PublishedGraphQualityHistoryConflict()
    issues = _issue_payloads(report)
    samples = _sample_payloads(report)
    if (
        properties["issue_record_count"] != len(issues)
        or properties["sample_record_count"] != len(samples)
        or properties["issue_manifest_json"] != _manifest(issues, "issue_id")
        or properties["sample_manifest_json"] != _manifest(samples, "sample_id")
    ):
        raise PublishedGraphQualityHistoryConflict()
    _child_rows(row.get("issues"), issues, id_key="issue_id")
    _child_rows(row.get("samples"), samples, id_key="sample_id")

    requirements = row.get("requirements")
    if isinstance(requirements, (str, bytes)) or not isinstance(
        requirements, Sequence
    ):
        raise PublishedGraphQualityHistoryConflict()
    acl_payloads: list[dict[str, Any]] = []
    for ordinal, raw in enumerate(requirements):
        if not isinstance(raw, Mapping) or not isinstance(raw.get("node"), Mapping):
            raise PublishedGraphQualityHistoryConflict()
        node = dict(raw["node"])
        if set(node) != {
            "requirement_id",
            "tenant_id",
            "run_id",
            "access_groups",
            "payload_hash",
        }:
            raise PublishedGraphQualityHistoryConflict()
        groups = _string_tuple(node["access_groups"])
        if tuple(sorted(groups)) != groups:
            raise PublishedGraphQualityHistoryConflict()
        if not (set(groups) & principal.groups):
            raise PublishedGraphQualityAuthorizationError()
        payload = {"access_groups": list(groups)}
        target = {
            "requirement_id": _ACL_PREFIX + _stable_hash([report.run_id, payload]),
            **payload,
            "tenant_id": report.tenant_id,
            "run_id": report.run_id,
            "payload_hash": _stable_hash(payload),
            "ordinal": ordinal,
        }
        if node != {key: value for key, value in target.items() if key != "ordinal"}:
            raise PublishedGraphQualityHistoryConflict()
        _edge(raw.get("edge"), {"ordinal": ordinal})
        if raw.get("relationship_count") != 1:
            raise PublishedGraphQualityHistoryConflict()
        acl_payloads.append(target)
    if (
        not acl_payloads
        or properties["acl_requirement_count"] != len(acl_payloads)
        or properties["acl_manifest_json"]
        != _manifest(acl_payloads, "requirement_id")
    ):
        raise PublishedGraphQualityHistoryConflict()
    authorization_hash = _stable_hash(
        {
            "tenant_id": report.tenant_id,
            "run_id": report.run_id,
            "acl_manifest_json": properties["acl_manifest_json"],
        }
    )
    if properties["authorization_hash"] != authorization_hash:
        raise PublishedGraphQualityHistoryConflict()

    publications = row.get("publications")
    if not isinstance(publications, Sequence) or len(publications) != 1:
        raise PublishedGraphQualityHistoryConflict()
    publication = publications[0]
    if not isinstance(publication, Mapping) or not isinstance(
        publication.get("node"), Mapping
    ):
        raise PublishedGraphQualityHistoryConflict()
    publication_node = publication["node"]
    if any(
        publication_node.get(key) != value
        for key, value in {
            "tenant_id": report.tenant_id,
            "publication_id": report.publication_id,
            "generation": report.publication_generation,
            "manifest_hash": report.manifest_hash,
            "ontology_version_id": report.ontology_version_id,
        }.items()
    ):
        raise PublishedGraphQualityHistoryConflict()
    _edge(
        publication.get("edge"),
        {
            "publication_generation": report.publication_generation,
            "manifest_hash": report.manifest_hash,
        },
    )
    tboxes = row.get("tboxes")
    if not isinstance(tboxes, Sequence) or len(tboxes) != 1:
        raise PublishedGraphQualityHistoryConflict()
    tbox = tboxes[0]
    if not isinstance(tbox, Mapping) or not isinstance(tbox.get("node"), Mapping):
        raise PublishedGraphQualityHistoryConflict()
    if any(
        tbox["node"].get(key) != value
        for key, value in {
            "tenant_id": report.tenant_id,
            "tbox_id": report.ontology_version_id,
            "checksum": report.tbox_checksum,
        }.items()
    ):
        raise PublishedGraphQualityHistoryConflict()
    _edge(tbox.get("edge"), {"tbox_checksum": report.tbox_checksum})

    expected_outgoing = Counter(
        {
            "AUDITS_KNOWLEDGE_PUBLICATION": 1,
            "USES_AUDITED_TBOX_VERSION": 1,
            "HAS_PUBLISHED_QUALITY_ISSUE": len(issues),
            "HAS_PUBLISHED_QUALITY_SAMPLE": len(samples),
            "REQUIRES_PUBLISHED_QUALITY_ACCESS": len(acl_payloads),
        }
    )
    expected_outgoing += Counter()
    expected_outgoing = Counter(
        {key: value for key, value in expected_outgoing.items() if value}
    )
    if Counter(row.get("outgoing_types") or ()) != expected_outgoing:
        raise PublishedGraphQualityHistoryConflict()
    if row.get("incoming_types") not in ([], ()):
        raise PublishedGraphQualityHistoryConflict()
    recorded_by = _required_text(properties["recorded_by"], "recorded_by")
    recorded_at = _aware_utc(properties["recorded_at"], "recorded_at")
    integrity_hash = _stable_hash(
        {
            "schema": PUBLISHED_QUALITY_HISTORY_SCHEMA_VERSION,
            "report_hash": report_hash,
            "recorded_by": recorded_by,
            "recorded_at": recorded_at.isoformat(),
            "authorization_hash": authorization_hash,
            "issue_manifest_json": properties["issue_manifest_json"],
            "sample_manifest_json": properties["sample_manifest_json"],
        }
    )
    if properties["integrity_hash"] != integrity_hash:
        raise PublishedGraphQualityHistoryConflict()
    return PublishedGraphQualityRun(
        report=report,
        recorded_by=recorded_by,
        recorded_at=recorded_at,
        record_hash=integrity_hash,
    )


def _summary(value: PublishedGraphQualityRun) -> PublishedGraphQualityRunSummary:
    report = value.report
    return PublishedGraphQualityRunSummary(
        run_id=report.run_id,
        tenant_id=report.tenant_id,
        publication_id=report.publication_id,
        publication_generation=report.publication_generation,
        ontology_version_id=report.ontology_version_id,
        corpus_revision=report.corpus_revision,
        graph_digest=report.graph_digest,
        ruleset_version=report.ruleset_version,
        passed=report.passed,
        total_issue_count=report.total_issue_count,
        total_error_count=report.total_error_count,
        issues_truncated=report.issues_truncated,
        counts=report.counts,
        recorded_by=value.recorded_by,
        recorded_at=value.recorded_at,
        record_hash=value.record_hash,
    )


class Neo4jPublishedGraphQualityHistoryService:
    """Audit the active publication and append immutable audit evidence."""

    def __init__(
        self,
        driver: SessionDriver,
        database: str = "neo4j",
        *,
        auditor: PublishedGraphQualityAuditor | None = None,
        clock: Callable[[], datetime] | None = None,
        transaction_timeout_seconds: float = 30.0,
    ) -> None:
        if driver is None:
            raise ValueError("driver must not be None")
        self.database = _required_text(database, "database", maximum=128)
        timeout = transaction_timeout_seconds
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or not 0.0 < float(timeout) <= _MAX_TRANSACTION_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "transaction_timeout_seconds must be finite and between 0 and 300"
            )
        if auditor is not None and not callable(getattr(auditor, "audit", None)):
            raise TypeError("auditor must implement audit")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self.driver = driver
        self.auditor = auditor or Neo4jPublishedGraphQualityService(
            driver,
            self.database,
        )
        self.clock = clock or (lambda: datetime.now(UTC))
        timeout_value = float(timeout)
        self._record_work = unit_of_work(
            metadata={
                "component": "graphrag-published-quality-history",
                "operation": "record",
            },
            timeout=timeout_value,
        )(self._record_tx)
        self._get_work = unit_of_work(
            metadata={
                "component": "graphrag-published-quality-history",
                "operation": "get",
            },
            timeout=timeout_value,
        )(self._get_tx)
        self._list_work = unit_of_work(
            metadata={
                "component": "graphrag-published-quality-history",
                "operation": "list",
            },
            timeout=timeout_value,
        )(self._list_tx)

    def audit(self, principal: Principal) -> PublishedGraphQualityReport:
        """Run and record an audit while preserving the auditor's API shape."""

        return self.audit_and_record(principal).report

    def audit_and_record(self, principal: Principal) -> PublishedGraphQualityRun:
        _require_quality_capability(principal)
        # Authorization/audit failures happen before a write session is opened.
        try:
            report = self.auditor.audit(principal)
        except PublishedGraphQualityError:
            raise
        except Exception as exc:
            raise PublishedGraphQualityHistoryUnavailable() from exc
        try:
            _report_document(report)
        except PublishedGraphQualityHistoryConflict:
            raise
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise PublishedGraphQualityHistoryConflict() from exc
        if report.tenant_id != principal.tenant_id:
            raise PublishedGraphQualityHistoryConflict()
        try:
            recorded_at = _aware_utc(self.clock(), "clock result")
        except PublishedGraphQualityHistoryConflict:
            raise
        except Exception as exc:
            raise PublishedGraphQualityHistoryUnavailable() from exc
        try:
            with self.driver.session(database=self.database) as session:
                return session.execute_write(
                    self._record_work,
                    principal,
                    report,
                    recorded_at,
                )
        except PublishedGraphQualityHistoryError:
            raise
        except PublishedGraphQualityAuthorizationError:
            raise
        except Exception as exc:
            raise PublishedGraphQualityHistoryUnavailable() from exc

    def get_run(
        self,
        principal: Principal,
        run_id: str,
    ) -> PublishedGraphQualityRun | None:
        _require_quality_capability(principal)
        identifier = _required_text(run_id, "run_id")
        try:
            with self.driver.session(database=self.database) as session:
                return session.execute_read(self._get_work, principal, identifier)
        except PublishedGraphQualityHistoryError:
            raise
        except PublishedGraphQualityAuthorizationError:
            raise
        except Exception as exc:
            raise PublishedGraphQualityHistoryUnavailable() from exc

    def list_runs(
        self,
        principal: Principal,
        *,
        publication_id: str | None = None,
        limit: int = 10,
    ) -> tuple[PublishedGraphQualityRunSummary, ...]:
        _require_quality_capability(principal)
        bounded_limit = _positive_limit(limit)
        publication = (
            None
            if publication_id is None
            else _required_text(publication_id, "publication_id")
        )
        try:
            with self.driver.session(database=self.database) as session:
                return session.execute_read(
                    self._list_work,
                    principal,
                    publication,
                    bounded_limit,
                )
        except PublishedGraphQualityHistoryError:
            raise
        except PublishedGraphQualityAuthorizationError:
            raise
        except Exception as exc:
            raise PublishedGraphQualityHistoryUnavailable() from exc

    def _record_tx(
        self,
        tx: Any,
        principal: Principal,
        report: PublishedGraphQualityReport,
        recorded_at: datetime,
    ) -> PublishedGraphQualityRun:
        report_document = _report_document(report)
        report_json = _canonical_json(report_document)
        report_hash = _stable_hash(report_document)
        lock = tx.run(
            _LOCK_BOUNDARY_QUERY,
            tenant_id=principal.tenant_id,
        ).single()
        if (
            lock is None
            or lock.get("tenant_id") != principal.tenant_id
            or lock.get("corpus_revision") != report.corpus_revision
        ):
            raise PublishedGraphQualityHistoryConflict()
        boundary = tx.run(
            _BOUNDARY_QUERY,
            tenant_id=principal.tenant_id,
            groups=sorted(principal.groups),
            publication_id=report.publication_id,
            publication_generation=report.publication_generation,
            manifest_hash=report.manifest_hash,
            ontology_version_id=report.ontology_version_id,
            tbox_checksum=report.tbox_checksum,
            corpus_revision=report.corpus_revision,
        ).single()
        if boundary is None:
            raise PublishedGraphQualityHistoryConflict()
        boundary_value = dict(boundary)
        if boundary_value.get("acl_complete") is not True:
            raise PublishedGraphQualityAuthorizationError()
        requirements = _normalize_acl_requirements(
            boundary_value.get("acl_requirements") or ()
        )
        if any(not (set(groups) & principal.groups) for groups in requirements):
            raise PublishedGraphQualityAuthorizationError()
        issue_values = _issue_payloads(report)
        sample_values = _sample_payloads(report)
        acl_values = _acl_payloads(report, requirements)
        issue_manifest = _manifest(issue_values, "issue_id")
        sample_manifest = _manifest(sample_values, "sample_id")
        acl_manifest = _manifest(acl_values, "requirement_id")
        authorization_hash = _stable_hash(
            {
                "tenant_id": report.tenant_id,
                "run_id": report.run_id,
                "acl_manifest_json": acl_manifest,
            }
        )
        integrity_hash = _stable_hash(
            {
                "schema": PUBLISHED_QUALITY_HISTORY_SCHEMA_VERSION,
                "report_hash": report_hash,
                "recorded_by": principal.principal_id,
                "recorded_at": recorded_at.isoformat(),
                "authorization_hash": authorization_hash,
                "issue_manifest_json": issue_manifest,
                "sample_manifest_json": sample_manifest,
            }
        )
        existing = tx.run(
            "MATCH (run:PublishedGraphQualityRun {run_id: $run_id}) "
            "RETURN run.run_id AS run_id LIMIT 2",
            run_id=report.run_id,
        ).data()
        if len(existing) > 1:
            raise PublishedGraphQualityHistoryConflict()
        if not existing:
            properties = {
                "run_id": report.run_id,
                "tenant_id": report.tenant_id,
                "record_schema_version": PUBLISHED_QUALITY_HISTORY_SCHEMA_VERSION,
                "record_kind": "AUDIT_EVIDENCE",
                "ruleset_version": report.ruleset_version,
                "publication_id": report.publication_id,
                "publication_generation": report.publication_generation,
                "manifest_hash": report.manifest_hash,
                "ontology_version_id": report.ontology_version_id,
                "tbox_checksum": report.tbox_checksum,
                "corpus_revision": report.corpus_revision,
                "graph_digest": report.graph_digest,
                "counts_json": _canonical_json(report_document["counts"]),
                "total_issue_count": report.total_issue_count,
                "total_error_count": report.total_error_count,
                "issues_truncated": report.issues_truncated,
                "passed": report.passed,
                "report_json": report_json,
                "report_hash": report_hash,
                "issue_manifest_json": issue_manifest,
                "sample_manifest_json": sample_manifest,
                "acl_manifest_json": acl_manifest,
                "issue_record_count": len(issue_values),
                "sample_record_count": len(sample_values),
                "acl_requirement_count": len(acl_values),
                "recorded_by": principal.principal_id,
                "recorded_at": recorded_at,
                "authorization_hash": authorization_hash,
                "integrity_hash": integrity_hash,
            }
            created = tx.run(
                _CREATE_RUN_QUERY,
                properties=properties,
                **{
                    key: properties[key]
                    for key in (
                        "tenant_id",
                        "publication_id",
                        "publication_generation",
                        "manifest_hash",
                        "ontology_version_id",
                        "tbox_checksum",
                    )
                },
            ).single()
            if created is None or created.get("run_id") != report.run_id:
                raise PublishedGraphQualityHistoryConflict()
            for query, values, id_key in (
                (_CREATE_ISSUES_QUERY, issue_values, "issue_id"),
                (_CREATE_SAMPLES_QUERY, sample_values, "sample_id"),
                (_CREATE_ACL_QUERY, acl_values, "requirement_id"),
            ):
                items = [
                    {
                        "ordinal": item["ordinal"],
                        "properties": {
                            key: value
                            for key, value in item.items()
                            if key != "ordinal"
                        },
                    }
                    for item in values
                ]
                if not items:
                    continue
                result = tx.run(query, run_id=report.run_id, items=items).single()
                if result is None or result.get("count") != len(values):
                    raise PublishedGraphQualityHistoryConflict()
        return self._load_authorized_tx(tx, principal, report.run_id)

    def _load_authorized_tx(
        self,
        tx: Any,
        principal: Principal,
        run_id: str,
    ) -> PublishedGraphQualityRun:
        authorization = tx.run(
            _AUTHORIZATION_QUERY,
            tenant_id=principal.tenant_id,
            run_id=run_id,
        ).single()
        if authorization is None:
            raise PublishedGraphQualityHistoryConflict()
        try:
            _authorize_row(dict(authorization), principal)
        except PublishedGraphQualityAuthorizationError:
            raise
        except PublishedGraphQualityHistoryConflict:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise PublishedGraphQualityHistoryConflict() from exc
        row = tx.run(
            _LOAD_QUERY,
            tenant_id=principal.tenant_id,
            run_id=run_id,
        ).single()
        if row is None:
            raise PublishedGraphQualityHistoryConflict()
        try:
            return _run_from_row(dict(row), principal)
        except PublishedGraphQualityHistoryConflict:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise PublishedGraphQualityHistoryConflict() from exc

    def _get_tx(
        self,
        tx: Any,
        principal: Principal,
        run_id: str,
    ) -> PublishedGraphQualityRun | None:
        exists = tx.run(
            "MATCH (run:PublishedGraphQualityRun {"
            "tenant_id: $tenant_id, run_id: $run_id}) "
            "RETURN count(run) AS count",
            tenant_id=principal.tenant_id,
            run_id=run_id,
        ).single()
        if exists is None or exists.get("count") == 0:
            return None
        if exists.get("count") != 1:
            raise PublishedGraphQualityHistoryConflict()
        return self._load_authorized_tx(tx, principal, run_id)

    def _list_tx(
        self,
        tx: Any,
        principal: Principal,
        publication_id: str | None,
        limit: int,
    ) -> tuple[PublishedGraphQualityRunSummary, ...]:
        rows = tx.run(
            _LIST_QUERY,
            tenant_id=principal.tenant_id,
            groups=sorted(principal.groups),
            publication_id=publication_id,
            limit=limit,
        ).data()
        values = tuple(
            _summary(
                self._load_authorized_tx(
                    tx,
                    principal,
                    _required_text(row.get("run_id"), "run_id"),
                )
            )
            for row in rows
        )
        expected_order = tuple(
            sorted(
                values,
                key=lambda value: (
                    -value.publication_generation,
                    -value.recorded_at.timestamp(),
                    value.publication_id,
                    value.run_id,
                ),
            )
        )
        if values != expected_order:
            raise PublishedGraphQualityHistoryConflict()
        return values


__all__ = [
    "Neo4jPublishedGraphQualityHistoryService",
    "PUBLISHED_QUALITY_HISTORY_CAPABILITY",
    "PUBLISHED_QUALITY_HISTORY_SCHEMA_VERSION",
    "PublishedGraphQualityAuditor",
    "PublishedGraphQualityHistoryConflict",
    "PublishedGraphQualityHistoryError",
    "PublishedGraphQualityHistoryUnavailable",
    "PublishedGraphQualityRun",
    "PublishedGraphQualityRunSummary",
]
