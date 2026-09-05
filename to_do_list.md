# To-do List

## Deferred

- [ ] Evaluate and add an independent reranker after the graph-expansion
  tuning is validated. Compare the current embedding-cosine candidate rerank
  against an established cross-encoder or provider reranker on the versioned
  retrieval dataset before selecting a model or changing production ranking.
  The current external-provider smoke run meets MRR/evidence-recall floors but
  not the stricter production-reference Recall@5 and nDCG@5 targets.
  The 2026-09-05 ad-hoc run also found a temporal/multi-company coverage gap:
  a query requesting Meridian and Harbor FY2024 risk evidence selected
  Harbor FY2023 instead of Harbor FY2024 in the final five Chunks. Keep this
  original failure as a regression example when evaluating ranked-versus-
  adjacent context selection and the deferred reranker; do not count a
  successful explicit Document-filter diagnostic as an unfiltered-query pass.

## Current work

- [x] Tighten the local Playground graph-expansion limits and verify retrieval
  quality, tenant/access isolation, bounded execution, and Retrieval Trace
  behavior. Completed with 49/49 authenticated HTTP paths, Gold-aware provider
  smoke metrics, and zero unauthorized exposure under the external
  `text-embedding-v4` profile.

- [x] Complete the industrial property-graph construction loop: versioned
  T-Box, authoritative A-Box import, document upload, ontology-constrained LLM
  extraction, candidate review, publication/rollback, trust-aware subgraph
  retrieval, permissions, audit, and Playground management views.

- [x] Add evidence-backed typed entity-property facts with server-normalized
  datatypes, units, and explicit validity/observation times.

- [x] Add versioned extraction quality and drift gates covering exact evidence,
  entities, relationships, typed properties, entity resolution, trust
  contamination, and human-review policy.

- [x] Connect conservative entity-resolution suggestions to the governed
  review workflow and Playground. Exact values for every declared
  `identity_property` can propose a unique authoritative link; ambiguous,
  incomplete, stale, or unauthorized matches fail closed. Applying a link
  creates immutable revisions and atomically rebinds dependent candidate
  assertions without approving those assertions.

## Live-acceptance checks

- [x] Diagnose and correct provider-backed upload/extraction/review/publication
  acceptance for `qwen3.8-max`. Default deep thinking exhausted the unchanged
  30-second provider bound. Explicit non-thinking execution and mechanical
  source-position hints passed a fresh real-model workflow: three entities,
  five mentions, two relationships and one typed property; 22 checks cover
  exact evidence, review/publication isolation, preserved SECONDARY authority,
  source-linked retrieval and tenant isolation. No model/key change, timeout
  extension, fabricated candidates or expert-import substitution was used.
  See `docs/validation/extraction-timeout-correction.md`; the earlier failed
  attempt remains historical evidence, not silently relabelled as a pass.

- [ ] Complete an actual browser visual/click acceptance pass when a browser
  runtime is available. Executable Node VM interaction tests and real HTTP
  checks pass, but this session could not obtain an in-app browser.

## Completed follow-ups

- [x] Recover once from a cold-start retrieval-store timeout during Playground
  warm-up, preserving the same read-only request, vector, ACL, limits and
  per-transaction deadline. No extra Embedding call or LLM retry is introduced;
  persistent failures still refuse startup.

- [x] Correct literal-object handling in the active A-Box inventory after a
  real mixed-publication smoke test exposed Cypher null-equality rejection.
  Valid literals require absent entity IDs and no OBJECT edge; forged IDs,
  empty-string substitutes and malformed entity objects still fail closed.
  See `docs/validation/inventory-literal-correction.md`.

- [x] Expose immutable published-graph quality audit history. The Playground
  offers explicit recording, publication-filtered history, and isolated
  historical details with original observer/time metadata. Live reads never
  persist automatically; request and identity guards prevent stale output.

- [x] Expose a bounded, ACL-complete active A-Box inventory in the Playground.
  It shows the exact publication/T-Box binding, trust and instance structure,
  relationship properties, and evidence locations without source text; an
  optional Document filter, immutable revision history, and stable-record
  publication-removal handoff make the active graph operationally inspectable.
  Executable UI checks cover request reordering, denied refresh, identity
  changes, invalid filters, and publication/rollback snapshot invalidation.

- [x] Separate immutable startup-fixture counters from runtime state in the
  Playground, show a fresh ACL-complete active Documents/Chunks summary for the
  selected JWT identity, and make readiness fail closed on missing/stale/multiple
  active generations or unavailable Neo4j vector indexes.

- [x] Make complete T-Box versions reusable from the Playground: load an exact
  checksum-bound definition, copy it to the next version for expert edits, or
  export a self-describing property-graph JSON artifact.

- [x] Add the independently authored and reviewed `semantic-holdout-v1` beyond
  the 49 builder-coupled questions. Its 14 balanced cases passed the recorded
  real-provider run with complete-evidence Recall@5 0.90, evidence-ID Recall@5
  0.8667, MRR 0.7583, and zero forbidden Chunk exposure.

- [x] Add a bounded active-publication graph-quality audit with complete ACL and
  exact T-Box binding, expose it through an independent `knowledge:quality`
  API/Playground card, and keep source text out of the response.

- [x] Expose governed active-document inventory and logical retirement through
  an independent `knowledge:lifecycle` API/Playground card. Retirement uses an
  active-snapshot/source-generation CAS plus a stable operation key, blocks on
  live knowledge or jobs, preserves immutable audit data, and refreshes the
  active vector generation before the local API returns.

- [x] Expose auditable active-publication record removal in the Playground,
  including removal-only change sets without directly deleting source data.

- [x] Add resumable knowledge-construction operations: bounded ACL-safe job
  status/list reads, immutable governed-record revision history, recoverable
  publication candidates, and Playground selection/retry controls with stable
  same-input operation keys.

- [x] Extend T-Box relationship-property definitions into evidence-backed,
  typed/unit/time-normalized extraction, authoritative import, review,
  publication, retrieval, API, and Playground instances. Publication also
  enforces relationship-property and closed-world endpoint cardinality.
