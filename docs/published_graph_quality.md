# Active Published Graph Quality

`Neo4jPublishedGraphQualityService` audits the graph that a tenant has actually
activated through `KnowledgePublication`. It complements the earlier snapshot
quality checker: it does not treat a candidate, an old teaching graph, or an
inactive publication as the live governed A-Box.

## Trust boundary

The internal service accepts `knowledge:quality` or the existing trusted review
capability for non-HTTP callers. The public `GET /v1/knowledge/quality` route is
stricter: it requires the independent `knowledge:quality` scope; review or
publication permission does not imply audit permission. Every audit requires complete access
to every source revision in the active publication. The database query applies
tenant and Chunk ACL predicates before returning metadata. If the publication,
its exact T-Box binding, an evidence path, or the caller's complete visibility
is ambiguous, the service fails closed. Source text is compared inside Neo4j
and is never included in the report.

The operation is bounded by server-owned limits for revisions, entities,
issues, review samples, anomalous hub degree, and transaction time. Reports
carry stable IDs and a graph digest so an unchanged active graph produces a
repeatable result.

The HTTP response contains only active publication/T-Box identifiers and
digests, fixed counts, pass/error totals, at most 1,000 issue metadata records,
and at most 20 deterministic sample objects with up to three evidence Chunk IDs
each. It never exposes source or quoted evidence text. The local Playground
renders the same bounded response in its **活动已发布图谱质量** card for full
steward identities.

## Checks

The report reconciles each immutable governed revision with its active
materialization and verifies:

- publication membership, current record heads, lifecycle state, authority,
  extraction metadata, and the exact published or retired T-Box version;
- Document, Version, Chunk, access policy, ACL, exact character range, and
  quoted evidence integrity;
- canonical entity identity, namespace, aliases, duplicate identity, isolated
  entities, and anomalous hubs;
- EntityMention and Assertion navigation IDs, fields, endpoints, typed literal
  values, membership edges, and supporting mention revisions;
- relationship-property stable IDs, typed/unit/time semantics, exact evidence,
  revision JSON, and materialized nodes;
- entity and relationship property cardinality plus closed-world relationship
  endpoint cardinality over the complete active publication.

Graph objects remain derived navigation data. A passing structural report does
not promote model output to authoritative knowledge and does not replace an
adjudicated semantic-precision evaluation or human review.

## Verification

```bash
uv run --locked python -m unittest tests.unit.test_published_quality -v
```

`tests/integration/test_published_quality_neo4j.py` runs against an explicitly
disposable loopback Neo4j database. It covers a repeatable clean report,
partial-ACL rejection, live navigation and relationship-property tampering,
endpoint-cardinality breakage, and missing active publication/T-Box bindings.
