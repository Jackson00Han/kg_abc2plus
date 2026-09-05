# Active inventory literal-object correction

Date: 2026-09-05. Scope: development-scale governed inventory correctness.

## Live finding

A newly published authoritative A-Box contained two entities, their
`INSTALLED_AT` relationship and a DECIMAL `RatedPower` fact of 11 kW, all backed
by one exact uploaded synthetic source Chunk. Publication and the independent
quality audit succeeded, but `GET /v1/knowledge/publication-inventory` returned
409. The literal assertion's materialization check produced zero valid paths.

The query compared two absent `object_entity_id` properties with `=`. Cypher
null equality does not evaluate to true, so the literal fact was incorrectly
rejected. This was a query defect, not an invalid source, ACL denial or model
response problem.

## Correction and invariants

The object check is now explicitly kind-dependent:

- Entity objects still require exactly equal entity IDs and the existing
  unique authorized OBJECT edge.
- Literal objects require both entity-ID properties to be absent and retain
  the zero-OBJECT-edge requirement. Empty strings and fabricated IDs are not
  accepted as substitutes for absence.

All manifest, publication/T-Box, tenant/ACL, evidence, typed literal,
relationship-property and in-transaction materialization checks remain intact.
No source or publication was edited to make the validation pass.

## Verification

The focused inventory unit suite passed 11 tests and all four disposable-Neo4j
inventory tests passed in 157.232 seconds. A new real-Neo4j regression
publishes authoritative mentions, an entity relationship and a typed literal
together. It freezes the earlier quality result, then verifies that empty or
fabricated literal entity IDs, an extra OBJECT edge and a missing entity-object
ID are rejected by the inventory transaction itself.

On the original live publication, a fresh application using the corrected code
returned HTTP 200 and all four records in 684 ms, preserving the 11 kW DECIMAL
semantics, authority and exact source locations without returning source text.
This used the existing Neo4j data and the HTTP contract; it did not rebuild the
corpus or mutate the active publication. The older running process still
required a restart to load the corrected module.

The post-correction full unit suite passed 631 tests; HTTP E2E passed 15,
security passed 33 and regression passed two. Final disposable-Neo4j totals
and the reviewed baseline are recorded in
`governance-workbench-completion.md` after the complete replay.

The independent live extraction timeout is a separate finding: it is not
fixed or concealed by this inventory correction.
