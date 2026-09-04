# To-do List

## Deferred

- [ ] Add a broader set of manually authored, non-Gold questions to evaluate
  real-world semantic retrieval behavior beyond the 49 reviewed fixtures.

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

- [x] Extend T-Box relationship-property definitions into evidence-backed,
  typed/unit/time-normalized extraction, authoritative import, review,
  publication, retrieval, API, and Playground instances. Publication also
  enforces relationship-property and closed-world endpoint cardinality.
