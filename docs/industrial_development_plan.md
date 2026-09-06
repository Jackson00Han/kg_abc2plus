# Schneider-oriented industrial knowledge workbench

## Authorized outcome

Build on the existing local service (currently `http://127.0.0.1:8002/playground`)
so a user can build, retrieve, and visually explore industrial knowledge. Start
with Canalis busbar trunking, then use EvoPacT HVX vacuum circuit breakers as a
second domain. Preserve the numbered teaching examples, published validation
history, and existing user data. This is development validation, not approval
for live electrical operations or a Schneider-certified diagnostic product.

## Invariants

- Separate equipment class, product family/model, and installed asset identity.
- Separate observed symptoms, candidate explanations, tests, and confirmed
  findings. Graph connectivity never proves causality.
- Separate classification, physical composition, electrical connection, and
  diagnostic relations. Layout depth is presentation, not factual authority.
- Bind technical knowledge to product, document revision, context, and exact
  source location. Keep original numbers, units, tables, and dates traceable.
- Preserve source origin, authority, review state, confidence, and applicability
  independently. Synthetic cases and curated reference material must never
  masquerade as actual Schneider field reports or company expert approval.
- Reuse governed revisions, source Chunks, JWT authorization, and bounded
  runtime services. Apply tenant/ACL filters within every retrieval/expansion.
- Do not silently change providers, lower quality gates, raise existing hard
  limits, or substitute fixture vectors for live model embeddings.
- Retain fast offline fixtures and a separate representative industrial corpus.
  Public originals may be fetched into an external cache; commit redistributable
  authored fixtures, source catalogs, checksums, and reproducible commands.

## Ordered milestones

Complete, validate, document, commit, and push each milestone before starting
the next implementation milestone. Parallel work inside one milestone may use
explicitly separated files. The root agent owns commits and service restarts.

| ID | Deliverables | Repeatable exit evidence |
| --- | --- | --- |
| I0 | Preserve current code and the existing deferred publication-UI note; record this plan | Baseline checks, diff/secret checks, checkpoint pushed |
| I1 | Product/source catalog, industrial task contract, bounded PDF normalization with source-location mapping, reproducible source acquisition | Source/version checks; PDF text/table/page provenance, malformed/oversized input checks; existing parser tests |
| I2 | Domain ontology and explicit hierarchy semantics; small Canalis governed flow, HVX extension, medium corpus and resumable bounded loading | Deterministic corpus/checksum/diversity checks; ontology and exact-evidence validation; real Neo4j lifecycle/publication/ACL tests |
| I3 | Fixed industrial retrieval gold; baseline assessment; measured product/entity/context improvements | Recall/MRR/nDCG plus applicability/negative/security checks; before/after evidence; no custom scoring |
| I4 | Reusable bounded graph-browsing API and separate frontend component; industrial same-port workbench, evidence inspection, corpus/construction/retrieval flows | API/security tests; node expansion/version/budget checks; browser interaction and visual QA |
| I5 | Complete regression, clean/restart/rebuild checks, live user walkthrough; running local service | Full relevant suites, reproducible industrial report, browser evidence, exact URL and instructions, final push |

## Working scope and acceptance

1. Select an explicit Canalis family and explicit HVX variant from verified
   official sources. Record regional/voltage/version exclusions. Source discovery
   is not proof that a document covers a diagnostic rule.
2. Use a small vertical fixture only to establish modeling correctness. Expand
   to dozens of related documents and hundreds of meaningful source Chunks;
   publish a separately counted, evidence-supported graph. Existing publication
   limits (currently 500 records) remain explicit. No claim that every source
   Chunk has an approved graph assertion.
3. Cover equipment identifiers, aliases/homonyms, parameters, observations,
   maintenance history, conflicts, unsupported conclusions, and authorization.
   Gold questions and evidence annotations must be independent of predictions.
4. PDF support must preserve page-level origin and exact normalized ranges;
   supported tables need traceable cell/row location. Scanned or ambiguous pages
   must return explicit limitations rather than invented text.
5. Initialize industrial identities and vector generations deliberately. Keep
   existing finance/legal test personas for regression, and present meaningful
   industrial personas in the new workbench.
6. Graph browsing and knowledge discovery must support authorized partial views
   without reusing the complete-ACL inventory as a general graph export. Every
   expansion is bounded and pins its publication/T-Box state.
7. The current page and a future UI use the same domain APIs and graph component.
   Display equipment structure, diagnostic relationships, and source evidence;
   preserve explicit state for secondary, provisional, and conflicting facts.
8. Final validation includes actual page usage. Live provider results and
   deterministic offline evaluation are recorded separately. An external outage
   is reported as a limitation, never concealed as successful semantic retrieval.

## Progress

- I0: checks passed; checkpoint commit/push records completion. Starting revision
  `260307d`; current service and its database are retained. Only `to_do_list.md`
  was modified before this task. See `docs/validation/industrial-checkpoint.md`.
- I0 checkpoint: `fd6dd13`, pushed to `origin/main`.
- I1: complete; official catalog, development contract, bounded PDF parsing,
  external-cache acquisition and exact normalization passed their checks. See
  `docs/validation/industrial-sources.md`.
- I2: complete; composed ontology/hierarchy, 34 authored documents, 330 semantic
  Chunks and resumable governed loading passed model, unit/API/security and
  real-Neo4j checks. Official excerpt preview totals 370 Chunks. See
  `docs/validation/industrial-model.md`.
- I3: complete. The 370-Chunk live load, frozen 72-case comparison, scoped
  retrieval and standard listwise reranking passed the primary acceptance gate:
  Recall@5 0.8635, MRR 0.9531, nDCG@5 0.8629, zero scope/ACL/citation errors.
  Holdout independently passes; development recall and two incomplete PDF
  contexts remain explicit limitations. See
  `docs/validation/industrial-retrieval.md`.
- I4–I5: pending; implementation begins after I3 is validated and pushed.

Detailed evidence is recorded under `docs/validation/industrial-*.md`. This plan
remains as design history; remove only the temporary active-work pointer from
`AGENTS.md` at completion.
