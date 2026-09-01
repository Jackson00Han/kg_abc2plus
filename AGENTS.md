# Production GraphRAG Development Plan

## Goal

Evolve the current GraphRAG teaching project into a production-oriented,
fully tested reference implementation. The target of this plan is the
"validation complete" milestone, not a live production deployment.

The system must preserve this evidence chain:

> trusted source -> versioned document -> traceable chunk -> governed graph
> -> bounded retrieval -> cited answer -> measurable validation

The existing numbered examples (`02` through `06`) remain as learning
artifacts. Production code must be organized into focused packages instead of
continuing to add all behavior to one numbered script.

## Non-negotiable Working Rules

1. Work through the stages below in order. Do not begin the next stage until
   the current stage satisfies its exit criteria.
2. Every implementation stage must include proportionate automated checks.
3. Before every commit, run the stage checks, `git diff --check`, and a secret
   scan of the files being committed.
4. Commit and push each completed stage separately to `origin/main`.
5. Never commit `.env`, credentials, generated caches, local databases, or
   virtual environments.
6. Preserve unrelated user changes and never rewrite published Git history.
7. Prefer established, documented retrieval and graph methods. Do not add a
   custom scoring formula without explicit approval and comparative evaluation.
8. Treat graph entities and relationships as derived navigation data. Source
   chunks remain the factual evidence used to generate answers.
9. Apply tenant and access filters during every recall and graph-expansion
   query, not only after retrieval.
10. Record material design decisions, test evidence, limitations, and metric
    changes in the repository.

## Definition of Done for Every Stage

A stage is complete only when all of the following are true:

- Its deliverables and tests are implemented.
- Its exit criteria are demonstrated by repeatable commands.
- Existing tests still pass.
- Formatting/static checks and `git diff --check` pass.
- No secrets or unintended generated files are included.
- Documentation reflects the implemented behavior.
- The stage has one focused commit pushed successfully to `origin/main`.

If a check fails, fix the problem and rerun the complete relevant check set
before committing. Do not hide failures by weakening or deleting tests.

## Stage 1: Requirements and Acceptance Contract

### Deliverables

- Define supported document types, expected corpus size, update frequency,
  question classes, tenancy model, and access-control boundary.
- Define representative test categories: single-chunk, cross-chunk, graph
  relationship, exact-value, temporal/conflicting, unanswerable, and
  unauthorized questions.
- Define measurable targets for retrieval quality, answer grounding, citation
  correctness, refusal behavior, latency, throughput, and ingestion success.
- Record assumptions separately from confirmed requirements.

### Checks

- Validate that every target has a measurement method and test dataset owner.
- Validate that every supported question class has positive and negative cases.
- Review the contract for undefined terms and untestable promises.

### Exit Criteria

- A versioned acceptance contract exists and all later stages can be judged
  against it.

## Stage 2: Production Data and Provenance Model

### Deliverables

- Add stable application IDs for Document, Chunk, Entity, and Relationship.
- Define document source, URI/path, publication time, ingestion time, version,
  checksum, tenant, and access metadata.
- Define chunk location, sequence, splitter version, embedding model/version,
  and source-document linkage.
- Store extraction provenance, evidence chunk IDs, extractor version, and
  confidence for derived entities and relationships.
- Add required uniqueness constraints and indexes.
- Stop using Neo4j internal `elementId` as a persistent business identifier.

### Checks

- Schema/constraint tests against a disposable Neo4j database.
- Provenance round-trip tests from relationship/entity to source text.
- Duplicate-ID and missing-required-metadata rejection tests.

### Exit Criteria

- Every derived fact and answer citation can be traced to an immutable source
  document version and exact chunk location.

## Stage 3: Idempotent Incremental Ingestion

### Deliverables

- Implement create, unchanged/no-op, update, and delete document lifecycles.
- Use content checksums and stable IDs to prevent duplicate ingestion.
- Update only affected chunks and graph elements when a document changes.
- Remove orphaned derived data safely when a source/version is deleted.
- Track ingestion jobs, errors, retries, and resumable state.
- Support embedding/index version migration without silently mixing vector
  spaces.

### Checks

- Repeat-ingestion idempotency test.
- Partial-update isolation test.
- Delete and orphan-cleanup test.
- Interrupted-ingestion recovery and transaction-boundary tests.

### Exit Criteria

- Repeating an operation produces the same final graph, and a failed operation
  cannot leave an unidentifiable partial state.

## Stage 4: Knowledge Graph Quality Governance

### Deliverables

- Define allowed entity labels, relationship types, properties, and patterns.
- Normalize names and manage aliases without merging distinct homonyms.
- Add established entity-resolution rules or models with auditable evidence.
- Reject or quarantine entities/relationships unsupported by source text.
- Detect duplicates, isolated nodes, invalid patterns, and anomalous hubs.
- Produce graph-quality reports and a reproducible human-review sample.

### Checks

- Entity and relationship precision tests on an adjudicated sample.
- Entity-resolution positive/negative pair tests.
- Constraint, orphan, duplicate, and unsupported-claim tests.

### Exit Criteria

- Graph-quality targets from Stage 1 are met, and every accepted relationship
  has source evidence.

## Stage 5: Production Retrieval Engine

### Deliverables

- Refactor file `06` behavior into reusable retrieval modules.
- Preserve vector and BM25 recall, standard RRF fusion, Resource Allocation
  candidate expansion, and adjacent-chunk context completion.
- Add deterministic relevance gating, deduplication, context budgeting,
  version filters, tenant/access filters, and configurable limits.
- Return stable citations and a structured retrieval trace.
- Ensure graph expansion is bounded and cannot bypass access control.

### Checks

- Unit tests for RRF, RA, deduplication, gates, and context budgets.
- Retrieval integration tests for all Stage 1 question classes.
- Access-control tests on initial recall, graph expansion, adjacency, and final
  context.
- Retrieval regression metrics: Recall@K, MRR, and nDCG.

### Exit Criteria

- Retrieval is bounded, explainable, permission-safe, and meets the agreed
  quality targets.

## Stage 6: Grounded Answer Generation

### Deliverables

- Require chunk-level inline citations for material factual claims.
- Return document provenance and exact chunk location with each citation.
- Treat graph data as navigation unless supported by source text.
- Implement insufficient-context refusal and conflicting-source behavior.
- Preserve original numbers, dates, currencies, and units.
- Separate sourced statements from explicitly labelled inference.

### Checks

- Answer correctness and citation-entailment tests.
- Unsupported-claim and citation-location tests.
- Unanswerable, conflicting-source, and numerical-fidelity tests.

### Exit Criteria

- Answers meet correctness, citation, and refusal targets and can be audited
  back to source text.

## Stage 7: API, Security, Reliability, and Observability

### Deliverables

- Provide APIs for ingestion, deletion, job status, retrieval, answering, and
  health/readiness checks.
- Add authentication, authorization, tenant isolation, validation, rate limits,
  timeouts, retries, and bounded concurrency.
- Configure Neo4j connection pooling and safe transaction handling.
- Add structured logs, request/trace IDs, metrics, error taxonomy, and secret
  redaction.
- Record latency, retrieval stages, token usage, model calls, and estimated
  cost without logging protected content by default.

### Checks

- API contract and end-to-end tests.
- Cross-tenant and unauthorized-access tests.
- Timeout, retry, dependency-failure, log-redaction, and health-check tests.

### Exit Criteria

- Requests are secure, bounded, diagnosable, and fail predictably when a
  dependency is unavailable.

## Stage 8: Automated Evaluation and Regression Gates

### Deliverables

- Build a versioned gold evaluation dataset with evidence annotations.
- Automate graph, retrieval, answer, citation, refusal, latency, and cost
  metrics.
- Add unit, Neo4j integration, API end-to-end, and regression test suites.
- Store baseline metrics and fail CI on agreed material regressions.
- Make evaluation runs reproducible by recording data, prompt, model, index,
  and configuration versions.

### Checks

- Verify metric implementations on small hand-computable fixtures.
- Run the complete suite twice to check reproducibility.
- Confirm negative and security cases cannot be excluded from reports.

### Exit Criteria

- A single documented workflow produces repeatable quality and regression
  reports for the complete system.

## Stage 9: Integrated Production-Candidate Validation

### Deliverables

- Test representative corpus sizes, concurrency, sustained load, and ingestion
  throughput.
- Test Neo4j, embedding-provider, and LLM latency, timeout, and failure modes.
- Validate interrupted ingestion, backup/restore, deletion completeness, access
  isolation, and recovery behavior.
- Measure latency percentiles, throughput, error rates, retrieval/answer
  quality, and operating cost.
- Produce a final validation report containing passed criteria, failures,
  limitations, residual risks, and deployment prerequisites.

### Checks

- Run the complete functional, quality, security, recovery, and performance
  suites against a clean environment.
- Reproduce the final report from committed configuration and documented
  commands.

### Exit Criteria

- All Stage 1 acceptance targets pass, or every exception is explicitly
  documented and accepted. The project is then a validated production
  candidate, not automatically a live production deployment.

## Planned Production Package Boundaries

The precise module names may evolve, but responsibilities must remain separate:

```text
src/
  graphrag_prod/
    domain/          # IDs, schemas, provenance, access model
    ingestion/       # document lifecycle and graph construction
    graph/           # constraints, resolution, quality checks
    retrieval/       # recall, fusion, expansion, gating, context
    generation/      # grounded prompts, citations, refusal
    api/             # request contracts and endpoints
    observability/   # logs, metrics, tracing
    evaluation/      # datasets, metrics, reports
tests/
  unit/
  integration/
  e2e/
  security/
  performance/
```

## Progress

| Stage | Status | Evidence |
| --- | --- | --- |
| Plan and execution rules | Complete | `AGENTS.md`; formatting and secret checks passed |
| 1. Requirements and acceptance contract | Complete | `contracts/acceptance.v1.json`; `docs/validation/stage-1.md` |
| 2. Production data and provenance model | Complete | `docs/provenance_model.md`; `docs/validation/stage-2.md`; 28 unit + 13 disposable-Neo4j tests |
| 3. Idempotent incremental ingestion | Complete | `docs/incremental_ingestion.md`; `docs/validation/stage-3.md`; 49 unit + 41 disposable-Neo4j tests |
| 4. Knowledge graph quality governance | Not started | |
| 5. Production retrieval engine | Not started | |
| 6. Grounded answer generation | Not started | |
| 7. API, security, reliability, observability | Not started | |
| 8. Automated evaluation and regression gates | Not started | |
| 9. Integrated production-candidate validation | Not started | |
