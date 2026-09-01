# Grounded Answer Generation

Stage 6 turns authorized retrieval context into a bounded, cited
`AnswerResult`. The reusable implementation lives in
`src/graphrag_prod/generation`; it does not query Neo4j, perform retrieval, or
use graph assertions as answer evidence.

The implementation preserves this part of the evidence chain:

> authorized RetrievedChunk -> integrity-checked source text -> untrusted
> answer plan -> validated claim and exact excerpt -> server-owned citation ->
> server-rendered answer

## Architecture and trust boundary

`GroundedGenerationService` receives a `GenerationRequest` containing a
question, an ordered tuple of `RetrievedChunk` values, and `GenerationLimits`.
Its processing order is:

1. Refuse immediately when retrieval supplied no context.
2. Enforce question, context-count, and context-character limits before a
   provider call.
3. Assign request-local citation labels `S1`, `S2`, and so on in retrieval
   order.
4. Require complete citation provenance and verify each Chunk's non-empty
   text, exact character-range length, and SHA-256 checksum.
5. Build the versioned prompt from the question, source text, and source
   provenance. Retrieval scores, graph traversal reasons, and graph facts are
   not included.
6. Call the injected provider-neutral `AnswerModel` boundary.
7. Treat the returned object as untrusted: validate its exact schema, labels,
   unique excerpts, authoritative source scopes, literals, material semantic
   operators, status shape, and output bounds.
8. Map referenced labels back to server-owned provenance and render the final
   answer with inline citations.

Authorization remains the retrieval engine's responsibility. Generation only
accepts the already authorized Chunks and does not re-query tenant or access
policies. It does re-check context integrity and never accepts provenance from
the model. Therefore callers must not bypass the production retrieval boundary
when constructing a `GenerationRequest`.

Source text and model output are both untrusted data. The prompt tells the
model not to follow instructions found in a source, while the server-side
validation prevents a source instruction from forging labels or changing a
label's provenance mapping. The validation is fail-closed, but its conservative
lexical and local-binding checks are not a general semantic-entailment or
prompt-injection classifier; the accepted-source boundary upstream remains
material.

The package currently defines only the synchronous `AnswerModel` protocol. It
does not contain a production LLM adapter, model selection, temperature,
credentials, timeouts, retries, cost accounting, or concurrency control. Those
operational concerns belong at the Stage 7 API/provider boundary.

## Prompt and provider output protocol

The current versions are:

- prompt: `grounded-answer-v1.3.0`;
- provider output schema: `grounded-answer-output-v1.0.0`.

`AnswerModelRequest` carries both versions. The prompt contains compact JSON
with the question and ordered sources. Each source includes its request-local
label, Chunk ID, Document ID and title, Version ID and number, source name,
canonical URI, publication time, exact location, and Chunk text. It excludes
retrieval scores, graph reasons, tenant/access metadata, and derived graph
facts.

The provider must return exactly one object with `status`, `claims`, and
`conflicts`; missing and unknown keys are rejected.

- `answered` requires at least one claim and no conflicts. Every claim has
  exactly `text`, `material`, `inference`, `citation_ids`, and `evidence`.
  `material` must be `true`.
- `insufficient_context` requires empty claims and conflicts.
- `conflict` requires empty top-level claims and one or more conflict objects.
  Each conflict has exactly `topic` and `alternatives`; each alternative has
  exactly `text`, `citation_ids`, and `evidence`.

Every evidence entry has exactly `citation_id` and `quote`. Each cited label
must be supplied in the prompt, every attached label must have at least one
evidence entry, and every quote must occur exactly once in that labelled Chunk.
The server expands each quote to its authoritative sentence/clause scopes. Each
attached citation must independently support the complete claim inside a
contiguous scope; combining words or literals across unrelated scopes is not
accepted. Repeated labels, repeated evidence, and canonically duplicate claims
are rejected. One source may still support multiple distinct claims. The model
may not place citation markers in claim text.

The model proposes claim text and source labels, but it does not render the
public answer. The server adds `[S<n>]` markers, adds the `Inference:` prefix
where required, orders referenced citations by retrieval order, and constructs
the conflict heading and claim groups. Invalid provider output becomes the
standard refusal with `failure_code=invalid_model_output`.

An exception raised by the provider itself is deliberately not converted into
an unanswerable refusal; it propagates to the caller. This prevents dependency
failure from being misreported as lack of evidence. Stage 7 must apply the
provider timeout, retry, and stable error policy.

## Claims, inference, conflicts, and exact values

For a sourced claim, ordered near-extractive wording, fact-bearing
prepositions, material leading/trailing qualifiers, negation, modality, bounds,
conjunctions, and exact literals must be supported inside one authoritative
scope for every citation. Scope edges may be omitted only by a small audited
allowlist for independently non-qualifying dates and corpus boilerplate.
Literal matching binds the surrounding subject and wording, so a value from
one fact cannot be moved to a different fact in the same Chunk. Unknown units,
rate bases, scenario/pro-forma qualifiers, and audit status fail closed rather
than disappearing from an accepted claim.

The only accepted runtime inference is a deterministic fiscal-year comparison
of the form `<measure> for <subject> increased|decreased|unchanged from fiscal
year YYYY to fiscal year YYYY`. The service locally binds subject, measure,
quantity, unit/currency, and year for both observations, rejects ambiguous,
bounded, approximate, ranged, accounting-parenthesized, reversed-year, or
same-year-conflicting inputs, and recomputes the direction. The final answer
always labels an accepted inference explicitly as `Inference:`.

Compatible values from different periods are not a conflict. For example, a
comparison of FY2023 and FY2024 is an `answered` result containing the two
sourced values plus a labelled comparison inference.

A `conflict` is reserved for incompatible accessible sources for which the
configured authority/version policy cannot select one. The generation layer
requires:

- at least two distinct sourced alternatives;
- no inference alternative;
- exact evidence for every citation;
- different Document/Version provenance across alternatives;
- a source-supported topic without numbers, dates, currencies, units, or
  citation markers; and
- for numerical alternatives, the same statement skeleton, explicit period,
  and comparable unit with genuinely different values; for text alternatives,
  the same subject/measure predicate with only its value differing; and
- conflict groups that cover every final claim exactly once.

The server renders the alternatives under `Conflicting source statements:`;
it does not infer which alternative is correct.

Recognized numbers, signed and accounting quantities, dates, fiscal years,
currencies, ranges, bounds, and units must remain locally bound and exact in
every attached citation. Rounding, conversion, sign removal, currency/unit
substitution, or introducing a new recognized literal is rejected. This gate
covers the committed test formats, including percentages, multi-word currency
names, scaled units, named dates, ISO-style dates, and fiscal years.

## Citation and result contract

`S1`, `S2`, and subsequent labels are deterministic within one request but are
not persistent business identifiers. Persistent audit identity comes from the
mapped `AnswerCitation`, which contains:

- Chunk ID and checksum;
- Document ID, canonical URI, source name, and document title;
- Version ID, checksum, and version number;
- Chunk ordinal, character start/end, page, and section; and
- timezone-aware publication time.

The model cannot author or modify these fields. `AnswerCitation.from_retrieval`
copies them from the authorized retrieval result and rejects a missing title or
publication time. Every returned citation must be referenced by a claim, every
claim must have a citation, and the set of rendered inline labels must exactly
match the structured result.

`AnswerResult` is immutable and has one of three statuses: `answered`,
`insufficient_context`, or `conflict`. `as_dict()` produces an API/evaluator
record with the status string and ISO-8601 publication times.

All insufficient-context results use the same public text:

```text
I don't have enough cited context to answer this question.
```

The optional `failure_code` distinguishes an evidence refusal from a system or
validation failure:

| Condition | `failure_code` | Provider called |
| --- | --- | --- |
| Empty context or provider-selected refusal | `None` | No / Yes |
| Missing or tampered retrieval provenance | `invalid_context` | No |
| Oversized question, context, or prompt | `generation_limit_exceeded` | No |
| Invalid or over-limit provider output | `invalid_model_output` | Yes |
| Provider exception | no `AnswerResult`; exception propagates | Yes |

Evaluation counts a refusal as correct only when `failure_code` is `None`.
Failure-coded results are generation failures and cannot raise refusal F1.

## Configuration and bounds

`GenerationLimits` is immutable and rejects zero, negative, non-integer, and
boolean values. Its defaults are:

| Limit | Default | Scope |
| --- | ---: | --- |
| `max_context_chunks` | 10 | Input Chunks |
| `max_context_chars` | 20,000 | Sum of Chunk text characters |
| `max_question_chars` | 2,000 | Question input |
| `max_prompt_chars` | 50,000 | Fully rendered provider prompt |
| `max_claims` | 20 | Answer claims or total conflict alternatives |
| `max_citations_per_claim` | 5 | One claim or alternative |
| `max_evidence_quotes` | 10 | One claim or alternative |
| `max_claim_chars` | 1,000 | Claim, alternative, or conflict topic |
| `max_evidence_quote_chars` | 5,000 | One exact excerpt |

Input and prompt limits are enforced before the provider call. Provider-output
limit violations are untrusted-output failures and produce
`invalid_model_output`. These bounds are independent of `RetrievalLimits`, so
increasing retrieval budgets does not implicitly increase generation budgets.

Prompt or output protocol changes require explicit version changes rather than
silently reusing the current prompt/schema identifiers.

## Evaluation and development profile

`datasets/dev-corpus-v1/answers.jsonl` is the independent answer gold for the
synthetic representative corpus. It binds all 49 question IDs to corpus
version `1.0.1`, answer-gold version `1.1.0`, exact evidence Chunks, material
claims, complete citation provenance, exact-value tokens, refusal behavior,
and recomputable temporal-comparison requirements. Sourced gold evidence is
limited to Chunks that directly support the claim, and the file contains no
stored predictions.

`scripts/evaluate_grounded_answers.py` requires an explicit `--results` JSONL
produced from actual `AnswerResult` values. It fails closed on missing, extra,
or duplicate cases and claims; stale corpus, gold, prompt, or output-schema
versions; duplicate provenance; non-canonical answer prose; unsupported extra
citations; and forbidden-answer exposure. Regression gates include complete
case correctness and zero generation failures, rather than allowing partial
denominators to hide omissions. The evaluator calculates supported-claim rate,
citation precision, citation coverage, numerical fidelity, refusal
precision/recall/F1, answer correctness, temporal-comparison handling,
conflict handling, generation failures, and forbidden-answer exposure.

Run the offline generation and evaluator tests with:

```bash
uv run --locked python -m unittest \
  tests.unit.test_generation \
  tests.unit.test_grounded_answer_evaluation -v
```

Evaluate a recorded result set with:

```bash
uv run --locked python scripts/evaluate_grounded_answers.py \
  --results /path/to/actual-answer-results.jsonl
```

The disposable-Neo4j integration uses the real ingestion, retrieval,
generation validation, citation mapping, and evaluator paths. Its
`_StaticAnswerModel` is a scripted adjudicated provider: it constructs the
expected structured payload from committed gold and the Chunks actually
retrieved. It does not call an external LLM and cannot measure model
instruction-following, natural-language answer quality, provider latency,
cost, or reliability.

This validation runs under `dev-mini`: 120 synthetic Chunks, capped local
Neo4j resources, and unchanged correctness, provenance, authorization,
refusal, and citation invariants. `dev-mini` results are smoke-only and are not
production-candidate evidence.

## Current limitations

- The 49-case corpus has 35 expected answers, 14 expected refusals, and **zero
  adjudicated unresolved conflicts**. Its `conflict_handling_rate` is therefore
  `None`, not a passing score. True same-scope conflict behavior is covered by
  isolated unit/service fixtures; Stage 8 must add representative conflict
  cases to the versioned gold dataset.
- The scope, ordered-token, semantic-operator, edge-omission, and local-literal
  gates are conservative safety checks, not complete semantic entailment. They
  reject some valid paraphrases, unfamiliar morphology, and unrecognized
  source layouts; adjudicated evaluation remains required.
- Literal recognition is regex-based and does not cover every locale, written
  number, table relationship, accounting convention, or unit expression.
- The deterministic conflict gate supports explicit numeric disagreements and
  narrow subject/predicate text-value disagreements. More complex logical
  incompatibility and authority-policy resolution remain upstream or
  adjudicated.
- The scripted provider and synthetic corpus do not validate an external LLM,
  customer data, multilingual behavior, production scale, latency, cost, or
  availability.
- Authentication, API authorization, rate limiting, provider timeout/retry,
  structured observability, and secret redaction are Stage 7 work and are not
  implemented by the generation package.
