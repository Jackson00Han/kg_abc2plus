# To-do List

## Deferred

- [ ] Evaluate and add an independent reranker after the graph-expansion
  tuning is validated. Compare the current embedding-cosine candidate rerank
  against an established cross-encoder or provider reranker on the versioned
  retrieval dataset before selecting a model or changing production ranking.
  The current external-provider smoke run meets MRR/evidence-recall floors but
  not the stricter production-reference Recall@5 and nDCG@5 targets.

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

## Completed follow-ups

- [x] Add the independently authored and reviewed `semantic-holdout-v1` beyond
  the 49 builder-coupled questions. Its 14 balanced cases passed the recorded
  real-provider run with complete-evidence Recall@5 0.90, evidence-ID Recall@5
  0.8667, MRR 0.7583, and zero forbidden Chunk exposure.

- [x] Add a bounded active-publication graph-quality audit with complete ACL and
  exact T-Box binding, expose it through an independent `knowledge:quality`
  API/Playground card, and keep source text out of the response.

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
