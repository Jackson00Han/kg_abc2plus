"""Read-only acceptance of the authorized topology upload and automatic review.

No source or model text, credentials, or unrelated knowledge is written to the
report. The job ID is taken from the browser upload receipt; every subsequent
query is restricted to its tenant and document/version or its exact review run.
Run only after the real upload has produced construction-response.json.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
MAX_RECORDS = 10000
MAX_AUDITS = 10000


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=str, allow_nan=False)


def checksum(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_rows(driver, database, query, **parameters):
    """All queries in this verifier are literal MATCH/RETURN statements."""
    from neo4j import READ_ACCESS
    with driver.session(database=database, default_access_mode=READ_ACCESS) as session:
        return session.execute_read(lambda tx: [dict(row) for row in tx.run(query, **parameters)])


def verify(driver, database, receipt):
    job_id = receipt.get("job_id")
    if not isinstance(job_id, str) or not job_id.strip():
        raise ValueError("construction receipt has no job_id")
    # This single exact-ID lookup establishes scope; no corpus enumeration.
    jobs = read_rows(driver, database, """
        MATCH (j:KnowledgeConstructionJob {job_id:$job})
        RETURN j.job_id AS job_id,j.tenant_id AS tenant,j.document_id AS document,
               j.version_id AS version,j.tbox_id AS tbox_id,j.status AS status,
               j.expected_chunks AS expected_chunks,j.completed_chunks AS completed_chunks
        LIMIT 2
    """, job=job_id)
    if len(jobs) != 1 or any(not jobs[0].get(key) for key in ("tenant", "document", "version")):
        raise ValueError("exact construction job scope is unavailable or ambiguous")
    job = jobs[0]
    scope = {key: job[key] for key in ("tenant", "document", "version")}
    outcomes = read_rows(driver, database, """
        MATCH (j:KnowledgeConstructionJob {tenant_id:$tenant,job_id:$job,
               document_id:$document,version_id:$version})
              -[:HAS_CHUNK_OUTCOME]->(o:KnowledgeConstructionChunkOutcome {tenant_id:$tenant})
        RETURN o.result_json AS payload LIMIT $limit
    """, **scope, job=job_id, limit=MAX_RECORDS + 1)
    if len(outcomes) > MAX_RECORDS:
        raise ValueError("construction outcome read limit exceeded")
    chunks = [json.loads(row["payload"]) for row in outcomes]
    expected_mentions = {rid for item in chunks for rid in item["mention_record_ids"]}
    expected_assertions = {rid for item in chunks for rid in item["assertion_record_ids"]}
    context_runs = read_rows(driver, database, """
        MATCH (:KnowledgeConstructionJob {tenant_id:$tenant,job_id:$job,
               document_id:$document,version_id:$version})
              -[:HAS_CONTEXT_PROJECTION]->(r:KnowledgeContextProjectionRun {tenant_id:$tenant,job_id:$job})
        RETURN r.manifest_json AS manifest,r.manifest_checksum AS checksum,r.record_ids AS ids LIMIT 17
    """, **scope, job=job_id)
    if len(context_runs) > 16:
        raise ValueError("context projection manifest limit exceeded")
    for projection in context_runs:
        manifest = json.loads(projection["manifest"])
        if checksum(projection["manifest"]) != projection["checksum"] or set(projection["ids"]) != set(manifest["expected_record_ids"]):
            raise ValueError("context projection manifest is incomplete or inconsistent")
        expected_assertions.update(projection["ids"])
    expected_records = expected_mentions | expected_assertions
    rows = read_rows(driver, database, """
        MATCH (h:KnowledgeRecordHead {tenant_id:$tenant})-[:CURRENT_REVISION]->(r {
            tenant_id:$tenant,document_id:$document,version_id:$version})
        WHERE r:GovernedEntityMentionRevision OR r:GovernedAssertionRevision
        OPTIONAL MATCH (o {tenant_id:$tenant,document_id:$document,version_id:$version,
                           record_id:r.record_id,revision:1})
        WHERE o:GovernedEntityMentionRevision OR o:GovernedAssertionRevision
        RETURN r.record_id AS record_id,r.revision_id AS revision_id,
               CASE WHEN r:GovernedEntityMentionRevision THEN 'ENTITY_MENTION' ELSE 'ASSERTION' END AS kind,
               r.governance_status AS status,r.reviewed_by AS reviewed_by,
               r.authority_level AS authority,r.origin AS origin,r.entity_id AS entity_id,
               r.entity_type AS entity_type,o.entity_id AS original_entity_id,
               o.canonical_name AS original_name,o.authority_level AS original_authority,
               o.origin AS original_origin,o.revision_id AS original_revision_id,
               r.evidence_char_start=o.evidence_char_start AND r.evidence_char_end=o.evidence_char_end
                 AND r.evidence_text=o.evidence_text AND r.chunk_id=o.chunk_id
                 AND r.access_policy_id=o.access_policy_id AND r.access_policy_version=o.access_policy_version
                 AND r.access_groups=o.access_groups AS original_evidence_preserved
        LIMIT $limit
    """, **scope, limit=MAX_RECORDS + 1)
    if len(rows) > MAX_RECORDS:
        raise ValueError("source record read limit exceeded")
    evidence = read_rows(driver, database, """
        MATCH (v:DocumentVersion {tenant_id:$tenant,document_id:$document,version_id:$version})
        MATCH (:KnowledgeRecordHead {tenant_id:$tenant})-[:CURRENT_REVISION]->(r {
            tenant_id:$tenant,document_id:$document,version_id:$version})
        WHERE r:GovernedEntityMentionRevision OR r:GovernedAssertionRevision
        OPTIONAL MATCH (v)-[:HAS_CHUNK]->(c:Chunk {tenant_id:$tenant,document_id:$document,
                                               version_id:$version,chunk_id:r.chunk_id})
        RETURN count(r) AS checked,
               sum(CASE WHEN c IS NULL OR r.evidence_char_start<c.char_start OR r.evidence_char_end>c.char_end
                   OR r.evidence_char_end<=r.evidence_char_start
                   OR substring(v.normalized_text,r.evidence_char_start,r.evidence_char_end-r.evidence_char_start)<>r.evidence_text
                   OR NOT any(g IN r.access_groups WHERE g IN c.access_groups)
                   THEN 1 ELSE 0 END) AS invalid
    """, **scope)
    publication = read_rows(driver, database, """
        MATCH (p:KnowledgePublication {tenant_id:$tenant})-[:PUBLISHES_KNOWLEDGE_REVISION]->(r {
            tenant_id:$tenant,document_id:$document,version_id:$version})
        RETURN count(DISTINCT p) AS publications,count(DISTINCT r) AS revisions
    """, **scope)[0]
    runs = read_rows(driver, database, """
        MATCH (:KnowledgeConstructionJob {tenant_id:$tenant,job_id:$job,
              document_id:$document,version_id:$version})
        MATCH (r:KnowledgeAutoReviewRun {tenant_id:$tenant,job_id:$job})
        RETURN r.run_id AS run_id,r.summary_json AS summary,r.status AS status,r.access_groups AS access_groups
        ORDER BY r.updated_at DESC LIMIT 2
    """, **scope, job=job_id)
    if not runs:
        raise ValueError("automatic review run is absent")
    run = runs[0]
    summary = json.loads(run["summary"])
    audits = read_rows(driver, database, """
        MATCH (:KnowledgeConstructionJob {tenant_id:$tenant,job_id:$job,
               document_id:$document,version_id:$version})
        MATCH (r:KnowledgeAutoReviewRun {tenant_id:$tenant,job_id:$job,run_id:$run})
              -[:HAS_AUTO_REVIEW_DECISION]->(a:KnowledgeAutoReviewDecision {tenant_id:$tenant,run_id:$run})
        RETURN a.kind AS kind,a.payload_json AS payload,a.checksum AS checksum,
               a.access_groups=r.access_groups AS access_preserved,a.reviewed_by AS reviewed_by
        LIMIT $limit
    """, **scope, job=job_id, run=run["run_id"], limit=MAX_AUDITS + 1)
    if len(audits) > MAX_AUDITS:
        raise ValueError("review audit read limit exceeded")

    source = json.loads((ROOT / "busway_files/topology.source.json").read_text())
    source_ids = {row[field] for collection, field in (
        ("assets", "asset_ref"), ("ports", "port_ref"), ("measurement_points", "point_ref"))
        for row in source[collection]}
    expected_objects = sum(len(source[name]) for name in ("assets", "ports", "measurement_points"))
    mentions = [row for row in rows if row["kind"] == "ENTITY_MENTION"]
    assertions = [row for row in rows if row["kind"] == "ASSERTION"]
    groups = defaultdict(set)
    reverse_groups = defaultdict(set)
    for row in mentions:
        groups[row["original_entity_id"]].add(row["entity_id"])
        reverse_groups[row["entity_id"]].add(row["original_entity_id"])
    auto = [row for row in rows if row["status"] == "APPROVED"
            and (row["reviewed_by"] or "").startswith("service:auto-review:")]
    auto_mentions = [row for row in auto if row["kind"] == "ENTITY_MENTION"]
    auto_assertions = [row for row in auto if row["kind"] == "ASSERTION"]
    items = summary.get("items", [])
    item_by_id = {item["record_id"]: item for item in items}
    applied_ids, model_attempts, valid_model_proposals = set(), 0, 0
    invalid_audits, audit_actor_errors = 0, 0
    for audit in audits:
        value = json.loads(audit["payload"])
        invalid_audits += checksum(canonical(value)) != audit["checksum"] or not audit["access_preserved"]
        audit_actor_errors += audit["reviewed_by"] != summary["reviewed_by"]
        if audit["kind"] == "APPLIED":
            applied_ids.update(record["record_id"] for record in value.get("records", []))
        if audit["kind"] == "MODEL_REVIEW":
            for attempt in value.get("audit", {}).get("attempts", []):
                model_attempts += 1
                valid_model_proposals += attempt.get("status") == "VALIDATED_PROPOSAL"
                if "request_checksum" in attempt:
                    invalid_audits += checksum(canonical(attempt["request"])) != attempt["request_checksum"]
                if isinstance(attempt.get("response"), str):
                    invalid_audits += checksum(attempt["response"]) != attempt["response_checksum"]

    actual_counts = {
        "entity_groups": len(groups),
        "approved_groups": len({row["original_entity_id"] for row in auto_mentions}),
        "approved_mentions": len(auto_mentions), "approved_assertions": len(auto_assertions),
        "manual_groups": len({row["original_entity_id"] for row in mentions
                              if item_by_id.get(row["record_id"], {}).get("decision") == "NEEDS_HUMAN"}),
        "manual_assertions": sum(item.get("record_kind") == "ASSERTION" and item.get("decision") == "NEEDS_HUMAN" for item in items),
        "blocked_assertions": sum(item.get("record_kind") == "ASSERTION" and item.get("decision") == "BLOCKED" for item in items),
        "incomplete": sum(item.get("decision") == "INCOMPLETE" for item in items),
    }
    count_mismatches = {key: {"reported": summary.get("counts", {}).get(key), "actual": value}
                        for key, value in actual_counts.items() if summary.get("counts", {}).get(key) != value}
    receipt_review = receipt.get("auto_review") or {}
    reasons = Counter((item["decision"], item["reason_code"]) for item in items if item["decision"] != "AUTO_APPROVED")
    state_mismatches = 0
    for row in rows:
        item = item_by_id.get(row["record_id"])
        if item is None:
            state_mismatches += 1
        elif item["decision"] == "AUTO_APPROVED":
            state_mismatches += not (row["status"] == "APPROVED" and row["reviewed_by"] == summary["reviewed_by"])
        elif row["status"] not in {"CANDIDATE", "QUARANTINED"}:
            state_mismatches += 1
    checks = {
        "construction_completed": job["status"] == "COMPLETED" and job["expected_chunks"] == job["completed_chunks"] == len(chunks),
        "all_constructed_records_preserved": {row["record_id"] for row in rows} == expected_records and len(rows) == len(expected_records),
        "all_source_object_identifiers_preserved": {row["original_name"] for row in mentions} == source_ids,
        "all_source_object_groups_preserved": len(groups) == expected_objects,
        "source_objects_not_split": all(len(values) == 1 for values in groups.values()),
        "distinct_source_objects_not_accidentally_merged": all(len(values) == 1 for values in reverse_groups.values()),
        "source_evidence_exact_and_complete": bool(evidence) and evidence[0]["checked"] == len(rows) and evidence[0]["invalid"] == 0,
        "immutable_evidence_preserved_after_review": all(row["original_evidence_preserved"] for row in rows),
        "source_authority_and_origin_unchanged": all(row["authority"] == row["original_authority"] and row["origin"] == row["original_origin"] for row in rows),
        "review_counts_match_current_records": not count_mismatches,
        "review_items_match_current_states": state_mismatches == 0,
        "upload_receipt_matches_persisted_review": receipt_review.get("run_id") == run["run_id"] and receipt_review.get("counts") == summary.get("counts"),
        "automatic_approvals_have_commit_audits": bool(auto) and {row["record_id"] for row in auto}.issubset(applied_ids),
        "model_review_audits_present_and_valid": valid_model_proposals > 0 and invalid_audits == 0 and audit_actor_errors == 0,
        "reported_model_calls_match_audits": summary.get("model_calls") == model_attempts,
        "automatic_review_completed_without_incomplete_items": summary.get("status") == "COMPLETED" and actual_counts["incomplete"] == 0,
        "instances_not_published": publication["publications"] == publication["revisions"] == 0 and all(row["status"] != "PUBLISHED" for row in rows),
    }
    return {
        "verified_at": datetime.now(timezone.utc).isoformat(), "read_only": True,
        "job_id": job_id, "tenant_id": scope["tenant"], "document_id": scope["document"],
        "version_id": scope["version"], "run_id": run["run_id"],
        "passed": all(checks.values()), "checks": checks,
        "expected_source_objects": expected_objects, "preserved_source_objects": len(groups),
        "distinct_current_entities": len(reverse_groups), "mention_records": len(mentions),
        "assertion_records": len(assertions), "current_statuses": {
            "mentions": dict(Counter(row["status"] for row in mentions)),
            "assertions": dict(Counter(row["status"] for row in assertions))},
        "actual_counts": actual_counts, "count_mismatches": count_mismatches,
        "auto_approval_ratio": {"entities": round(len({r["original_entity_id"] for r in auto_mentions}) / max(1, len(groups)), 4),
                                "mentions": round(len(auto_mentions) / max(1, len(mentions)), 4),
                                "assertions": round(len(auto_assertions) / max(1, len(assertions)), 4)},
        "remaining_reason_counts": [{"decision": decision, "reason_code": reason, "count": count}
                                    for (decision, reason), count in sorted(reasons.items())],
        "model_calls": model_attempts, "valid_model_proposals": valid_model_proposals,
        "audit_kinds": dict(Counter(audit["kind"] for audit in audits)),
        "invalid_audits": invalid_audits, "source_instance_publications": publication["publications"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / ".local/browser-qa/auto-review-upload/construction-response.json")
    parser.add_argument("--output", type=Path, default=ROOT / ".local/auto-review-acceptance.json")
    args = parser.parse_args()
    if not args.input.is_file():
        print(json.dumps({"passed": False, "error_code": "UPLOAD_RECEIPT_NOT_READY"}))
        return 2
    from dotenv import load_dotenv
    from neo4j import GraphDatabase
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / ".env.workbench.local", override=True)
    try:
        uri = os.environ["PLAYGROUND_NEO4J_URI"]
        if urlsplit(uri).hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("acceptance is restricted to local Neo4j")
        receipt = json.loads(args.input.read_text())
        with GraphDatabase.driver(uri, auth=(os.environ["PLAYGROUND_NEO4J_USER"], os.environ["PLAYGROUND_NEO4J_PASSWORD"]),
                                  connection_timeout=15, max_transaction_retry_time=0) as driver:
            report = verify(driver, os.getenv("PLAYGROUND_NEO4J_DATABASE", "neo4j"), receipt)
    except Exception as error:
        # Neo4j/provider exception text can contain connection details. Keep
        # public failures bounded and never emit credentials or source content.
        report = {"passed": False, "read_only": True, "error_code": "ACCEPTANCE_READ_FAILED",
                  "error_type": type(error).__name__}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
