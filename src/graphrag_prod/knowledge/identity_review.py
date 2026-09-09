"""Generic human identity decisions, independent of domain identifier fields."""

from dataclasses import replace
from hashlib import sha256
import json

from graphrag_prod.domain.ids import entity_id

POLICY_VERSION = "human-identity:v1"


def independent_entity(mention):
    # Keep the declared namespace; never invent a business identifier or use a name
    # as the identity key. The source record and revision make retries deterministic.
    namespace = mention.entity.canonical_key.partition(":")[0]
    digest = sha256(json.dumps([POLICY_VERSION, mention.tenant_id,
        mention.trust.ontology_version_id, mention.record_id,
        mention.revision.revision], ensure_ascii=False).encode()).hexdigest()
    key = f"{namespace}:reviewed-{digest}"
    return replace(mention.entity, canonical_key=key,
        entity_id=entity_id(mention.tenant_id, mention.entity.entity_type, key))


def identity_actions(candidate, definition, suggestions):
    namespace = candidate.entity.canonical_key.partition(":")[0].casefold()
    allowed = namespace in definition.canonical_key_namespaces
    outcomes = {item.outcome.value for item in suggestions}
    return {"independent": {"allowed": allowed,
        "reason_code": "HUMAN_JUDGMENT" if allowed else "NAMESPACE_REQUIRES_CORRECTION",
        "note": "可依据原文确认为独立实体；相似候选不代表身份相同。" if allowed else
                "当前实体身份格式不符合知识模型，请先修正或归入合法实体。",
        "requires_reason": True},
        "policy_version": POLICY_VERSION,
        "match_state": "UNCERTAIN" if "CONFLICT" in outcomes else
                       "CANDIDATES" if outcomes & {"AUTO_LINK", "REVIEW"} else "NO_CANDIDATES"}


def impact_digest(rows):
    return sha256(json.dumps(sorted((row["record_id"], row["revision"])
        for row in rows), separators=(",", ":")).encode()).hexdigest()


def suggestion_reason_code(suggestion):
    if suggestion.outcome.value != "CONFLICT":
        return suggestion.outcome.value
    if "missing identity" in suggestion.reason:
        return "IDENTITY_EVIDENCE_MISSING"
    if "boundary" in suggestion.matcher_version:
        return "INVALID_BOUNDARY"
    if "multiple" in suggestion.reason or "more than one" in suggestion.reason:
        return "AMBIGUOUS_TARGETS"
    return "IDENTITY_REVIEW_REQUIRED"
