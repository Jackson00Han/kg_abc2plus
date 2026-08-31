# Production-Candidate Acceptance Contract

Contract version: 1.0.0  
Machine-readable source: `contracts/acceptance.v1.json`  
Target milestone: validation complete

## Purpose

This contract defines what this repository means by a validated production
candidate. It intentionally does not claim that one local validation run proves
fitness for every deployment. A deployment outside the validation envelope
must establish and test its own capacity and service-level objectives.

The first-principles boundary is simple: the system may only return knowledge
that an authorized caller can trace to an accepted source version. Graph data
is derived navigation data; source chunks are evidence.

## Confirmed Requirements

The repository owner has explicitly required the following:

- Preserve the progressive tutorial examples while building separate
  production-oriented modules.
- Use established retrieval and graph methods instead of unvalidated custom
  scoring formulas.
- Include vector and lexical retrieval, graph expansion, and grounded answer
  generation.
- Work stage by stage, test every stage, and push one focused commit only after
  its checks pass.
- Continue through integrated validation without pausing for intermediate
  product decisions.

## Explicit Working Assumptions

No deployment-specific business requirements were supplied, so this contract
uses conservative, testable assumptions:

- The initial authoritative input is UTF-8 plain text. A PDF or web page is
  supported only after an upstream extractor supplies text plus stable page or
  section provenance.
- The reference domain is English-language company filings. The data model is
  domain-extensible, but multilingual quality is outside this validation.
- Logical multi-tenancy is required. Every document belongs to one tenant and
  has an access-group list; callers may only retrieve allowed documents.
- Update visibility is eventual, with one active document version used for
  ordinary retrieval and older versions retained only when policy allows it.
- The validation environment is a single API service and a Neo4j database. It
  validates at least 10,000 chunks and eight concurrent retrieval clients.
- External LLM and embedding availability is not controlled by this service.
  Dependency timeouts and failures must be bounded and visible.
- The maintainers of this repository own the gold dataset until a business
  domain owner is assigned.

These assumptions are requirements for this reference implementation, not
universal claims. Changing one requires a new contract version and regression
run.

## Supported Document Lifecycle

The validated lifecycle includes:

1. Create a new document and immutable version.
2. Re-submit identical content as a no-op.
3. Replace content by creating a new version and changing the active version.
4. Delete or revoke a document without leaving retrievable derived data.
5. Retry an interrupted job without duplicating documents, chunks, entities,
   relationships, or embeddings.

Each accepted source must have a stable application ID, content checksum,
version, tenant, access groups, origin, ingestion timestamp, and exact chunk
locations.

## Supported Question Classes

The gold set must contain both expected-success cases and boundary/negative
cases for every class:

| Class | Expected-success example | Boundary/negative example |
| --- | --- | --- |
| Single chunk | Retrieve one product statement | Similar vocabulary in the wrong section |
| Cross chunk | Combine revenue and cash facts | One required fact is absent |
| Graph relationship | Connect a company to products or risks | Shared high-frequency entity is irrelevant |
| Exact value | Preserve a revenue, date, or percentage | Nearby but incorrect number competes |
| Temporal/conflicting | Prefer the requested document version | Sources disagree or no time is specified |
| Unanswerable | Refuse an out-of-domain question | An answerable paraphrase must not be refused |
| Unauthorized | Authorized principal gets cited evidence | Unauthorized principal gets no evidence |

The machine-readable contract sets minimum case counts. Gold cases must name
their expected document and chunk evidence rather than only an answer string.

## Quality Gates

The authoritative thresholds and measurement definitions are stored in
`contracts/acceptance.v1.json`. They cover:

- Graph entity, relationship, and entity-resolution quality.
- Retrieval Recall@5, MRR, nDCG@5, and unauthorized exposure.
- Supported-claim rate, citation precision/coverage, numerical fidelity, and
  refusal F1.
- Ingestion success, idempotency, deletion completeness, and recovery.
- Retrieval and answer latency, retrieval throughput, and server error rate.

Quality metrics use versioned, adjudicated fixtures. Latency and throughput
metrics use the declared reference profile, warmed indexes, fixed concurrency,
and recorded software/configuration versions. External model latency is
reported separately from retrieval latency.

## Security Boundary

- The authenticated principal supplies a tenant ID and access groups through a
  trusted server-side identity mapping, never through an unchecked query field.
- Tenant/version/access predicates apply to vector recall, BM25 recall, graph
  expansion, adjacent chunks, citations, and document APIs.
- A forbidden document must contribute neither text nor graph-derived hints,
  scores, citations, logs, or existence signals.
- Logs exclude credentials and protected source content by default.

## Out of Scope for This Milestone

- A specific cloud deployment, regional topology, or 24/7 on-call process.
- Legal/compliance certification.
- OCR, image understanding, table extraction, and arbitrary binary formats.
- Multilingual quality guarantees.
- Proof of performance beyond the declared validation envelope.
- Automatic acceptance of model-extracted facts without source evidence.

## Validation Evidence

Every stage records repeatable commands and test results. The final report must
identify the contract version, source/gold dataset versions, graph schema,
splitter, embedding model, LLM/prompt version, index configuration, hardware,
test timestamp, passed metrics, failures, accepted exceptions, and residual
risks.

Run the Stage 1 contract checks with:

```bash
python scripts/validate_acceptance_contract.py
python -m unittest tests.unit.test_acceptance_contract
```

