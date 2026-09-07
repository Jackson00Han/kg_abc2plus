# I5 industrial service walkthrough

This final development milestone builds on I4 commit
`74d1c4dc3c6d6afdf6c4067a8a79688e9c9806c2`. Its live checks use the retained
local service and real configured providers. Complete disposable-database
regression and baseline maintenance are recorded separately in
[industrial-final-regression.md](industrial-final-regression.md).

Status: complete. Live recovery, review, publication, final main-directory
service restart and both complete regression executions passed. The focused
I5 commit and push record delivery.

## Existing industrial baseline

The reproducible load has 34 authored documents and 330 meaningful Chunks,
plus two official-manual excerpt documents containing 40 Chunks. The resulting
36-source, 370-Chunk baseline has 193 published governed records: 117 mentions
and 76 assertions. It models Canalis KT and the selected EvoPacT HVX variant,
with eight explicitly registered synthetic assets. Source originals, normalized
locations, document revisions and checksums remain independently traceable.

The project-curated reference is pending enterprise expert review. Synthetic
field records are explicitly labelled; these are not Schneider field reports
or certified diagnostic findings. Publication does not promote secondary facts
to authoritative knowledge.

## Fresh live retrieval

Two browser requests used the current service, fresh provider calls and the
locked I3 retrieval profile. No cached evaluation rankings were substituted.

| Scope and question | Observed source context | Service time |
| --- | --- | --- |
| Canalis KT / BKT-A01: first temperatures/current and whether they are manufacturer limits | The five results include 48.2 °C, 29.1 °C and 910 A as synthetic observations, then comparability, event and baseline context | 7.148 s |
| EvoPacT HVX / HVX-A01: closing failure, control conditions, follow-up and a coil-fault conclusion | The five results include the recorded mode-gating conclusion, withdrawn coil hypothesis, observation, candidate explanations and verification context | 5.902 s |

These are two functional examples, not a latency percentile or a new quality
benchmark. The two listwise calls reported 11,502 total tokens; embedding
billing is not inferred. The page returns source evidence; final answer
generation remains disabled. The renderer and source text were visually checked.

The independently frozen I3 72-case report remains the quality evidence:
Recall@5 0.8635, MRR 0.9531 and nDCG@5 0.8629, with no recorded applicability,
ACL or citation errors. Its development-only recall miss and incomplete
contexts in two of three PDF questions remain recorded. Rechecking that report
does not constitute a new model evaluation.

## Live extraction failure and controlled correction

The first browser upload used the committed synthetic registration example,
the maintenance persona, Canalis KT family scope and maintenance-only access.
Its source ingestion succeeded, but its one Chunk was rejected after two
strict validation attempts. The completed job is
`ea254881-a560-5dd4-8013-60d40ba46087`; the failed artifact remains auditable.
No candidate or published fact was created from that extraction.

The initial model response was complete, but wrapped JSON in Markdown. Its
one corrective response removed the wrapper while retaining an invalid local
reference, inaccurate source coordinates and an unsupported literal copied
from ontology guidance. This was not response truncation.

A single bounded control changed only the existing response-format option
from `none` to `json_object`, keeping the same source, ontology, prompt and
two-call budget. It eliminated the JSON-format failure but still failed strict
validation. This control also exposed a separate Chinese word-boundary false
rejection: a literal present verbatim in a continuous Chinese sentence was
incorrectly treated as part of an unrelated alphanumeric word. The control
performed no database writes and consumed 21,793 reported tokens in 28.482 s.
Its record checksum is
`eebb4aa77d55041923e62e0bda0475083e181b14292d93a1409aee6ab3130974`.

The correction is versioned rather than overwriting the original failed job.
It aligns the declared reference grammar with the validator, provides bounded
exact-quote coordinate candidates to the model, and distinguishes Chinese
script boundaries from numeric/code token boundaries. Hints never choose a
meaning, edit source text, repair a response in place or replace strict
validation. Ontology examples do not count as source literals. All original
call, output, evidence, endpoint, authorization and review budgets remain.

The first real v6 request then exposed a downstream inconsistency. Its second
model response passed strict validation, but `ProvenanceBundle` still used a
duplicated older STRING boundary rule and rejected the response during parent
artifact assembly. The API returned 503 after 41.266 s; this was an internal
validation mismatch, not evidence of an unavailable model or database. Both
model attempts remain durably stored under job
`4a8c983a-d54c-539f-828a-e46f112e1bf5`; no candidate was published by that failed
request. The two calls took 32.163 s in total.

The common rule now lives in `domain/source_tokens.py` and is used consistently
by extraction, typed relationship properties, provenance, governed assertions
and graph projections. The narrow exception applies only to an explicitly
typed STRING raw value containing assigned CJK ideographs. Units, temporal
tokens, numeric values, mixed codes and untyped literals retain their strict
boundaries. In particular, a unit substring cannot use the STRING exception.
Presence of a Chinese word inside a negated sentence does not prove an
affirmative claim; source meaning still requires review.

Interrupted construction can recover a uniquely complete saved validation run.
The bounded reader checks the current source lifecycle and ACL before reading
attempts. Recovery verifies identities, checksums, scope, sequence and findings,
then runs the saved response through the current strict validator before
assembling the parent artifact. It retains the original attempt chain and uses
the terminal attempt's recorded time. It never accepts the stored status alone.
Missing/competing chains and an oversized response that was not retained require
explicit conflict handling. A valid incomplete provider-failed run can still
start a new bounded attempt; its earlier audit remains intact.
Zero additional calls applies to a complete, durably saved response. If a
provider processes a call but its response is lost before audit persistence,
an explicitly resumed bounded run may incur another charge; this mechanism
does not provide globally exactly-once provider billing.

A read-only rehearsal recovered the exact stored v6 response into 8 mentions
and 9 assertions, all `CANDIDATE / SECONDARY`, with zero provider calls and zero
database writes. A separate AI review checked all nine assertions against the
297-character synthetic source. It preserved the explicit project-configuration
and non-catalog-model qualifiers, and found no rated parameter, fault conclusion
or operating instruction. These are local development checks, not enterprise
expert approval.

## Actual recovery, review and publication

The browser submitted the exact original v6 request and operation key after an
HTTP-only restart. The same job completed in 5.993 s, producing eight candidate
mentions and nine candidate assertions. Both original attempt artifacts,
including their raw responses and timestamps, remained byte-for-byte identical
in the canonical property capture. The new parent audit
`0fb34d49-e43e-52d0-b19f-ad4adbe6b07d` references those same two attempts;
its output checksum is
`3510aafa33bbf9127bf871f0bb092e15e0f4cb06d484fd41c53799fb934e22d3`.
No additional model attempt was created.

Using the administrator persona, the browser inspected all eight entity
resolution results. The Canalis KT mention was explicitly linked to
`industrial:family-canalis-kt` after comparing the complete uploaded source
with the project's Canalis reference and product scope. This was a review
decision, not automatic name-only merging. The seven other mentions retained
their separate synthetic identities. All nine assertion dependency assessments
were ready, and every approval recorded a local AI development review note.
This exercise uses different local roles, not two independent human reviewers
or enterprise SME approval.

The browser selected exactly the 17 approved revisions and published them with
the prior active-publication compare-and-swap value. No removal or replacement
was requested. Publication generation 2 is
`6b760d0c-e4e5-56d0-ac71-88d633e8cd86`: all 193 original revision identities
remain published, alongside 17 new secondary records, for 210 total.
The refreshed neighborhood contained seven nodes, six relationships and five
source-backed literal properties. Browser review and publication actions
reported no JavaScript errors. Screenshots and the inventory comparison are
retained in `v6-resume/` under the external evidence directory.

The browser reopened the new model, expanded its neighborhood and selected
`ModelKind`. Its evidence response and on-screen highlight agree exactly on
source characters 147–184. Origin remains `LLM_EXTRACTED`, authority remains
`SECONDARY`, and the local AI review note is present. A public persona could
read neither the known new asset's graph neighborhood nor the known uploaded
Chunk; both returned the same empty shape used for inaccessible content.

After withdrawing the first failed test source, a fresh API request with
`include_graph=true` ranked the successful upload first. The full 297-character
source retains its synthetic and non-catalog-model qualifiers. The retired
source is absent from recall, final citations and graph output. The response
contains five source Chunks and a separately cited subgraph with nine entities,
eight relationship assertions and five literal assertions, all pinned to
publication generation 2 and corpus revision 39. It took 29.308 s overall;
the one reranking call took 4.858 s and reported 6,245 tokens. This is a single
functional combined-API observation, not a latency target or new retrieval
quality evaluation. The workbench's ordinary search requests omit the optional
subgraph. Lower-ranked passages include broader product context; their presence
does not turn them into facts about the new synthetic equipment.

The recovery regression also exposed a current-job authorization ordering
issue. A revoked job could reach ingestion replay and then be rejected as a
lower-level idempotency conflict. The workflow now checks the stored job's
tenant and current group authorization before preparing or replaying ingestion.
The regression requires no extra source preparation, provider calls, candidate
writes or audit changes after denial, and successful reuse after access is
restored. Source and Chunk permissions remain independently checked.

## Data preservation and scope

Before live writes, two independent read-only captures agreed on the protected
legacy scope: 12 documents, 1,167 nodes and 2,266 relationships, with zero
explicit cross-tenant relationships. Its canonical property digest was
`979c2f08833e00cb0be58bf9959e6c668e14b7ba13431f8929db43ea96f139bc`.
The original industrial source/chunk digest was
`8381cb9161ac19e0e9806adb280487fe8949c4f70d1ed03eedb5d9c067f52745`.
These are declared-scope preservation checks, not full database backups.

The first failed v5 upload was explicitly retired using the administrator API
and its current snapshot/generation precondition. Retirement
`92af403a-1a66-5519-84ae-01fc41735333` removes it from active retrieval while
retaining its DocumentVersion, Chunk, completed rejection job, parent audit and
both original model-attempt artifacts. The successful v6 source remains active.

Two consecutive post-walkthrough captures agree: the original industrial
source/chunk digest and protected legacy digest are unchanged, and no explicit
cross-tenant relationship exists. There are 37 active industrial sources and
371 active Chunks with 371/371 vector coverage. Historical totals are 38 source
Documents and 372 Chunks, including the one retired test upload. The active
publication contains 210 records; all 193 original published revision IDs were
also compared individually and preserved. This is incremental preservation,
not replacement of the seeded graph.

Raw provider responses, API captures and screenshots are retained outside Git
under `/tmp/graphrag-industrial-i5`. Credentials, originals, caches and local
databases are excluded from commits. Development validation does not amend
the historical Stage 9 production-candidate qualification.

## Main-directory restart and final browser checks

The HTTP process was restarted from the final main working directory using
`--enable-industrial --reuse-existing-corpus --no-open --port 8002
--skip-provider-warmup`. The retained database was not restarted, reset or
reseeded. Both health routes returned 200; readiness confirms Neo4j, active
embedding generations and vector indexes. `/industrial` and `/playground`
both return their pages. This restart makes no fresh provider calls.

Two consecutive read-only captures after restart agree with the completed
walkthrough: unchanged original sources, protected legacy data and active
publication; zero document, Chunk or corpus-revision delta; complete 371/371
embedding coverage. The final browser exercised 27 checks, including both
product families, all relation views, ontology inspection, exact node/edge
source highlights, source adjacency, four personas, PNG export and 1024/390
pixel page-width checks. All passed, with no browser JavaScript or console
errors. Desktop, mobile and published-literal evidence screenshots were also
visually inspected.

A separate browser check reopened the published synthetic model property,
verified its exact 147–184 source range and local AI review note, and confirmed
that the public persona cannot read its known entity or Chunk. The published
revision remains secondary. These are read-only checks; no extra construction,
review, publication or retrieval model request was needed after restart.

The external browser harness first read the initial static node count before
rendering and then, after adding a render wait, failed to allow a valid empty
view. Both failures are retained in `main-restart/first-browser-attempt.json`
and `second-browser-attempt.json`. The harness now waits for either a rendered
graph or its explicit empty state. The application's implementation and the
expected default six-node graph were unchanged.

Selected external evidence checksums (paths relative to
`/tmp/graphrag-industrial-i5`):

| Artifact | SHA-256 |
| --- | --- |
| `live-after-main-restart.json` | `17cf4ab2d94881e4b33dcd755713a1f1d47961d2fd48e41d355d6cb14d1b9579` |
| `main-restart/health.json` | `7a8ef0d76712f8e542ea38d4f5d7bcab5cf43f598c8d7e098ae4cc2edd975e97` |
| `main-restart/browser-readonly-report-v1.json` | `e7e7217e31c00a6fc48e184794a9a9870ac46a82aeebd0964bc6a4806b3138a2` |
| `main-restart/published-evidence-report.json` | `c14772a91e929c1da475f73ffd4e82413c9b168a17fbf00baed5f5400819be35` |
| `v6-resume/saved-response-recovery-proof.json` | `433d553f521d1dda28a12c84e9c6c0c9f09acef8e3f4490963a31195c4d64f9e` |
| `v6-resume/publication-report.json` | `715ab14551dbd010bd2c77d6a63843513945f97fbd483d9315aaa097dda5cac2` |
