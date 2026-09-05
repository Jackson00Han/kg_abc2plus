# Active Publication A-Box Inventory

## Purpose

`Neo4jActivePublicationInventoryService` provides a bounded operational view
of the A-Box records in the tenant's unique active `KnowledgePublication`. It
answers “what governed entities and assertions are active now?” without
treating mutable `Entity`, `EntityMention`, or `Assertion` nodes as an
independent source of truth.

The immutable governed revision and its exact publication manifest remain the
authority. Source Chunks remain factual evidence. The inventory exposes only
evidence locations; it never returns Chunk text, `evidence_text`, mention
surface text, or relationship-property evidence text.

## Read contract

The service requires a principal with `knowledge:quality` or
`knowledge:review`. A read succeeds only when all of these conditions hold:

1. the tenant has exactly one active publication and one exact bound T-Box;
2. the public active-publication quality audit passes for the principal's
   complete evidence ACL;
3. publication edges exactly equal the immutable revision-ID manifest;
4. every revision is `PUBLISHED`, tenant-local, and the current revision of its
   logical record head;
5. every evidence range resolves through the active document version and
   active `KnowledgeSnapshot`, with matching ACL metadata and source text;
6. every active navigation materialization and snapshot membership agrees with
   its immutable governed revision; and
7. every linked canonical entity is tenant-local and agrees with the revision;
8. relationship-property JSON agrees with every materialized value's stable
   ID, typed fields, ordered assertion edge, and exact evidence Chunk/range.

The quality audit and inventory projection use separate read transactions, so
the projection transaction deliberately reloads and validates the complete
manifest. It never validates only the requested output page. A concurrent
publication change or direct graph mutation therefore fails closed instead of
returning a mixed snapshot.

## Bounded output

- A request limit must be between 1 and 500.
- The complete active manifest is hard-capped at 500 records for this
  operational endpoint. A larger publication returns
  `ACTIVE_PUBLICATION_INVENTORY_LIMIT_EXCEEDED`; it is not silently sampled.
- The projection transaction requests at most 501 rows to detect an exceeded
  or corrupted bound.
- Ordering is stable: record kind, logical record ID, then revision ID.
- Optional `document_id` filtering happens in memory only after the complete
  authorized manifest has been validated. Forbidden and absent documents
  cannot be distinguished through a partial graph query.
- `total_record_count`, `matching_record_count`, and `truncated` make output
  pagination behavior explicit.

## Safe item projection

Each item contains:

- logical record ID, revision ID, record kind, governance status;
- origin, authority level, confidence, and exact ontology type or predicate;
- document/version/Chunk IDs and exact character start/end;
- the source Chunk ordinal for direct evidence navigation;
- for a mention: canonical entity ID, type, canonical key, and display name;
- for an assertion: a canonical subject plus either a canonical entity object
  or a structured literal value; and
- for typed literals: datatype, typed/canonical value, canonical unit, and
  normalized temporal bounds when present; and
- for relationship properties: stable value ID, property name, confidence,
  literal semantics, and its own exact evidence location.

The HTTP projection is `GET /v1/knowledge/publication-inventory`, with optional
`document_id` and `limit` (default 100, maximum 500). It requires the independent
`knowledge:quality` JWT scope even though the core service also accepts the
review capability. The response intentionally omits its internal tenant field.
It is read-only and retry-safe in the common bounded HTTP worker runtime.

Literal fact values are graph facts, not source passages. Raw evidence strings,
review notes, aliases, prompts, and source text are intentionally omitted.

## Failure behavior

Authorization, publication conflict, safety-limit, and dependency-unavailable
errors use fixed public messages. Neo4j messages and protected data remain in
exception causes for controlled logs only. Any partial ACL, missing or multiple
active publication, stale record head, mismatched publication membership,
cross-tenant entity link, stale source version, or tampered materialization
rejects the entire read.

## Validation

Repeatable focused checks:

```sh
.venv/bin/python -m unittest tests.unit.test_published_inventory -v
.venv/bin/python -m unittest tests.unit.test_api_knowledge \
  tests.e2e.test_knowledge_api tests.security.test_knowledge_api_security -q

TEST_NEO4J_URI=bolt://127.0.0.1:17699 \
TEST_NEO4J_USER=neo4j \
TEST_NEO4J_PASSWORD='<disposable-password>' \
TEST_NEO4J_DATABASE=neo4j \
GRAPHRAG_ALLOW_DISPOSABLE_DB=1 \
.venv/bin/python -m unittest \
  tests.integration.test_published_inventory_neo4j -v
```

The Neo4j suite must run only against an empty disposable loopback database.
