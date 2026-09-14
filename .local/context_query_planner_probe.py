"""Read-only cold-plan diagnostic; run only against an owned disposable test DB."""
import hashlib
import json
import os
from pathlib import Path
from time import perf_counter

from neo4j import GraphDatabase

from graphrag_prod.knowledge.review import ReviewRecordKind, _REVIEW_QUERY
from graphrag_prod.knowledge.store import context_evidence_guard

if os.environ.get("GRAPHRAG_ALLOW_DISPOSABLE_DB") != "1":
    raise SystemExit("An explicitly owned disposable database is required.")

parameters = {"tenant_id": "tenant-industrial", "groups": ["board", "public"],
              "statuses": ["CANDIDATE", "APPROVED", "PUBLISHED"], "limit": 100}
query = _REVIEW_QUERY[ReviewRecordKind.ASSERTION]
guard = context_evidence_guard(version="version", snapshot="snapshot", document="document", primary="chunk")
variants = (
    ("baseline_without_context_guard", "CYPHER replan=force EXPLAIN " + query.replace("AND " + guard, "")),
    ("context_with_source_partition", "CYPHER replan=force EXPLAIN " + query),
    ("greedy_diagnostic_only", "CYPHER replan=force connectComponentsPlanner=greedy EXPLAIN " + query),
)
measurements = []
with GraphDatabase.driver(os.environ["TEST_NEO4J_URI"],
        auth=(os.environ["TEST_NEO4J_USER"], os.environ["TEST_NEO4J_PASSWORD"]),
        notifications_min_severity="OFF") as driver:
    with driver.session(database=os.environ.get("TEST_NEO4J_DATABASE", "neo4j")) as session:
        for name, cypher in variants:
            started = perf_counter()
            result = session.run(cypher, **parameters)
            rows = sum(1 for _ in result)
            summary = result.consume()
            measurements.append({"variant": name, "query_sha256": hashlib.sha256(cypher.encode()).hexdigest(),
                "wall_seconds": round(perf_counter()-started, 3), "server_ms": summary.result_available_after,
                "result_rows": rows})

payload = {"mode": "EXPLAIN only; no source records or credentials are returned", "measurements": measurements,
           "parameters": parameters, "row_equivalence": "EXPLAIN does not execute queries and cannot prove functional equivalence."}
Path(os.environ.get("CONTEXT_PLAN_OUTPUT", ".local/context-query-planning-reproduction.json")).write_text(
    json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(payload, ensure_ascii=False))
