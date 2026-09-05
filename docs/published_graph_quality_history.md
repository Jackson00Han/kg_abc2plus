# Published Graph Quality History

`Neo4jPublishedGraphQualityHistoryService` adds an immutable evidence history
around the active-publication quality auditor. It does not change the audited
publication, approve a revision, or mark a failing graph as trusted.

## Write boundary

The service requires the dedicated `knowledge:quality` capability and complete
access to every ACL set observed by the active publication audit. It first runs
`Neo4jPublishedGraphQualityService.audit` in its existing bounded read
transaction. An audit exception or authorization failure opens no history write
transaction.

The write entry point is explicitly `audit_and_record`; the history service has
no `audit` alias and cannot be substituted for the read-only auditor. A transport
can supply the optional `report_validator(report)` constructor callback to
validate its complete report projection after core validation and before the
clock or a write session is opened. Rejected projections therefore create no
history record. HTTP wiring must use this callback when its response schema is
stricter than the reusable core, including when injecting a custom auditor.
Invalid projection values fail with the history conflict error; other callback
failures use the redacted history unavailable error.

The core stores at most 5,000 issue records, 200 review samples, 128 named count
entries, and 10,000 distinct ACL requirements per run. The default auditor emits
at most 1,000 issues and 20 samples; HTTP additionally uses its fixed six count
keys. A direct service/CLI caller can retain the larger core limits. History is
never silently truncated to fit a transport. `TimeoutError` remains unchanged
across audit, validation, clock, write, and read boundaries so an HTTP adapter
can report its existing timeout status rather than treating it as unavailability.

The second transaction uses the same transient `KnowledgePublicationState`
write lock as the publication workflow and also locks the tenant corpus-state
node. Both lock properties are removed inside the transaction and are never
persisted. The exact publication
must still be the tenant's sole active publication, its immutable manifest and
T-Box identifiers/checksums must match the report, and the corpus revision must
not have advanced. If that boundary changed between read and write, the service
rejects the stale observation and records nothing. Consequently, a stored run
is an observation bound to that exact publication/T-Box state; it is not a
claim that a later publication or graph still has the same quality result.

The quality auditor's deterministic `run_id` is the history identity. The first
successful write stores `recorded_by` and `recorded_at`. A later authorized
expert auditing the unchanged graph receives the original first-observed
metadata: replay validates the exact report and all history edges and is a
no-op. Per-invocation telemetry, if needed, belongs in a separate invocation
log rather than mutating this evidence record.

## Immutable representation

Each `PublishedGraphQualityRun` stores the canonical report, fixed counts,
pass/error totals, report and integrity digests, first observer, and time. It is
bound by exactly one `AUDITS_KNOWLEDGE_PUBLICATION` edge and one
`USES_AUDITED_TBOX_VERSION` edge. Issues, deterministic review samples, and
the audit-time ACL requirements use separately constrained immutable nodes and
ordinal edges.

Replay and reads verify exact property sets, payload hashes, child manifests,
edge properties, edge cardinality, child degree, publication/T-Box identity,
and the record integrity hash. Missing, extra, redirected, or modified history
data fails closed. This detects accidental or out-of-band field/edge changes;
it is not a substitute for externally signed/WORM audit storage against a
database administrator who can rewrite both data and hashes.

The stored report is allowed to have `passed = false`. Recording that result
preserves evidence of a defect and never promotes the publication, revisions,
entities, or relationships to a higher authority level.

## Read boundary

`get_run` and `list_runs` require `knowledge:quality`, match the exact tenant,
and require the caller to intersect every recorded ACL requirement. Authorization
is checked again from the same loaded record that is decoded, so an earlier
prefilter can never authorize a later, changed ACL payload. The list is
bounded to 50 records, can be restricted to one publication, and is stable by
publication generation descending, observation time descending, publication
ID, then run ID. Returned records contain metadata, issue descriptions, and
evidence Chunk IDs only; source, Chunk, quoted, or extracted evidence text is
never stored in this history.

## Verification

```bash
uv run --locked python -m unittest \
  tests.unit.test_published_quality_history \
  tests.unit.test_schema -v
```

`tests/integration/test_published_quality_history_neo4j.py` runs only against
an explicitly disposable loopback Neo4j database. It covers schema replay,
first-observer idempotency, persisted failing reports, stale-boundary refusal,
ACL isolation, stable filtered history, and property/child/binding-edge
tampering. Unit and integration checks also require rejected response
projections to leave history empty, and unit checks verify timeout propagation.
