# Industrial demo initialization and automatic resolution

Date: 2026-09-06. This maintenance adds a source-backed industrial exercise to
the local Playground and improves entity-resolution ergonomics. It does not
change retrieval scoring, promote model knowledge to expert authority, or renew
the historical Stage 9 production-candidate qualification.

## Implemented behavior

- A committed, versioned `industrial-demo-v1` provides a pump-maintenance T-Box,
  a 309-character expert-source document, a 153-character maintenance report,
  and a 151-character same-name/different-code example. Each fits in one normal
  Chunk and has a checked SHA256. Seven expert records contain exact evidence
  for three entity mentions, two relationships and two typed property facts.
- The workbench downloads these public synthetic files and fills document
  metadata. After an exact SOURCE_ONLY source upload, it binds the actual
  returned document/version/Chunk/T-Box IDs into an editable expert import.
  Import and publication are still separate explicit user actions. A changed
  file, wrong mode or incomplete response cannot silently generate this draft.
- `extraction_mode=SOURCE_ONLY` uses the ordinary governed parse/chunk/embed
  lifecycle, permissions, active T-Box and resource limits. It never creates an
  extractor or model proposals. A durable profile-bound audit distinguishes
  source-only work from LLM work. Legacy LLM replay stays compatible; switching
  modes with the same operation key conflicts. A fresh LLM operation can reuse
  the same source/version/Chunk/snapshot and embeddings.
- Step 04 automatically computes entity-resolution suggestions through at most
  two concurrent GET requests. Identity, queue, revision and request guards
  discard stale results; only suggestion panels update, preserving selection,
  edits and focus. Manual refresh and authority/T-Box publication or rollback
  refresh matching context. Explicit reviewed linking, fact approval and
  publication remain separate.
- The [Chinese manual walkthrough](../industrial_demo_walkthrough.md) covers
  initialization, extraction, alignment, review, publication, evidence
  retrieval, same-name rejection, ACLs, rollback and governed retirement.

## Automated checks

The final complete `dev-mini` development cycle passed all 890 tests: 707 unit,
15 HTTP E2E, 33 security, two regression and 133 disposable-Neo4j integration
tests, with zero errors, failures or skips. Unit tests took 212.966 seconds;
the integration suite took 879.890 seconds. This is one complete final-code
cycle, separate from earlier intermediate checks and later report replays.

Baseline `1.5.0` adds 58 unit and eight integration test IDs to `1.4.0`, with
no prior IDs removed or renamed. All 160 case digests, 20 contract metrics,
diagnostics and schema/quality identities remain unchanged. The final semantic
digest is
`692c8b935cacd0e3370d172651c76d88f569b9a2023fc65301dfc9d1c6860fae`.
The independent review and two exact report replays are recorded alongside the
release evidence; replay is distinct from rerunning the suites.

Gold/corpus rebuilds, acceptance validation, lock verification, package build,
bytecode compilation, shell syntax and `git diff --check` passed. The built
wheel includes all seven demo assets byte-for-byte. Local release evidence is
retained in `/tmp/graphrag-industrial-demo-release-validation`; focused database
results are `/tmp/graphrag-identity-integration-results.json` (four tests,
138.413 seconds) and `/tmp/graphrag-validation-feedback-integration-results.json`
(five tests, 253.136 seconds). These temporary artifacts are not committed.

The focused source-only checks passed 91 unit/HTTP/security tests and three
disposable-Neo4j construction tests. The real database checks include legacy
job replay, no extractor or candidate writes in SOURCE_ONLY, explicit expert
import/publication, source recovery, mode conflicts, ACL isolation and reuse of
the same source/embedding identities by a later LLM operation.

Nine offline kit tests validate source hashes, exact ranges, ontology/API
contracts, a concrete A-Box batch, typed properties, relationship endpoint
containment and positive/negative identity matches. Thirteen executable Node
checks cover automatic-resolution scheduling, refresh, stale-response handling
and explicit mutation boundaries. Ten demo-page tests cover downloads,
runtime-ID binding, changed-file refusal, upload mode, duplicate submission and
identity changes, and bounded per-attempt summaries without raw output.

Repeat the complete development gate from a fresh output directory:

```sh
./scripts/run_stage8_validation.sh \
  --output-dir /tmp/graphrag-industrial-demo-new-validation --repeat 1
```

The default remains two cycles when `--repeat` is omitted. Focused checks:

```sh
uv run --locked python -m unittest \
  tests.unit.test_industrial_demo \
  tests.unit.test_playground_demo_ui \
  tests.unit.test_playground_resolution \
  tests.unit.test_construction_validation_feedback \
  tests.unit.test_construction_workflow \
  tests.unit.test_entity_resolution \
  tests.unit.test_api_knowledge \
  tests.unit.test_playground
```

## Browser acceptance evidence

Chromium `140.0.7339.16` loaded the real `8002/playground` service using
`Tenant Alpha · Finance + Legal`. The local report is
`/tmp/graphrag-ui-qa.Rd97ZF/report.json`; the adjacent `check.cjs` records the
executed assertions. The browser loaded the example T-Box into the editor and
verified its `pump-maintenance-demo` key. Each file's preparation button filled
the expected source URI and SOURCE_ONLY/LLM mode while leaving the file picker
empty for the human to select the download. All three actual browser downloads
matched the manifest at that pass, before the maintenance-entry simplification
described below. The historical maintenance download was the original
185-character sample, not the current 153-character file:

| Download | SHA256 |
| --- | --- |
| `authoritative_source.txt` | `33d43135ca3f1bf81e57097bfeecb4f6d014c1c9807bced5d876d113395f6422` |
| `maintenance_report.txt` (original 185-character sample) | `b7563697240481461e143d34ccde4a5c78e4fd60b354110b2276d05b934706b2` |
| `homonym_report.txt` | `1c5f55a4bcb90fd86bc37227eb30c896fdfb5faace3404c52f474102cd39c782` |

At viewport widths 1440 and 768, document scroll widths were exactly 1440 and
768 respectively, with no horizontal overflow. The captured viewport images
`knowledge-1440.png` and `knowledge-768.png` show the governance header, source
cards and download/preparation controls adapting to those widths. They are
viewport screenshots, not proof of every lower-page interaction. No JavaScript
page errors or attempted blocked mutation requests were observed. The browser
guard allowed session creation and retrieval requests while preventing other
non-GET/HEAD operations; this browser pass did not upload, import, approve,
apply entity links or publish knowledge.

A separate page intercepted review-queue and resolution GET responses with
three isolated mock candidates. Its observed maximum matching concurrency was
two, and editor contents, selection checkboxes, the edit-enable checkbox and
keyboard focus survived asynchronous suggestion updates. The corresponding
`review-mock-1440.png` is UI scheduling evidence only: its displayed entities
and matching suggestions are synthetic responses, not evidence of live Neo4j
matching or a browser-driven knowledge lifecycle. The report and three images
remain local temporary QA artifacts; this committed summary records their
scope and results.

After the starter report was finalized, a second real-browser pass against the
restarted `8002` service downloaded the current 309/153/151-character files.
All bytes matched the current manifest; the maintenance SHA256 was
`84a62a94a14c7121bf86c5f2178ef499c132e12202a818a9c6e2f2ea83123ab8`.
The card showed 153 characters, all three preparation buttons selected the
correct URI/mode, file selection remained explicit, and there were zero
JavaScript errors or protected mutation attempts. Evidence is in
`/tmp/graphrag-ui-qa.Rd97ZF/final-download-report.json` and the visually checked
`final-download-cards.png`. The original browser artifacts were retained.

## Initial live extraction failure and prompt correction

The first live API attempt is preserved in
`/tmp/graphrag-industrial-demo-live-first-attempt.json`. It successfully
published the T-Box, ingested the authoritative source in SOURCE_ONLY mode,
imported and published all seven expert records, and passed the authority
graph-quality check with zero issues. The maintenance upload then returned
HTTP 200 for a completed construction request but a **REJECTED** Chunk result.
Its durable audit recorded `ENDPOINT_OUTSIDE_EVIDENCE` at
`$.property_facts[1].entity_ref`: the property evidence did not enclose a
declared subject mention. No mention or assertion candidates from that
rejected extraction were written. Therefore the first attempt failed the
`maintenance_extracted_without_findings` check; HTTP success was not counted
as extraction success.

That attempt used `qwen3.8-max` and prompt
`industrial-property-graph-extraction:v2-token-spans` on the original
185-character maintenance report, before the later entry simplification. The
prompt correction, versioned as
`industrial-property-graph-extraction:v3-declared-mentions`, explicitly asks
the model to declare every source occurrence used by an endpoint or property
subject, and to enclose an actual declared mention inside each fact's
evidence. Reusing an entity ref does not imply an undeclared occurrence of its
name. This clarifies the existing contract; it does not broaden exact-evidence
validation, modify the source text, change the model, or increase resource
limits.

The bounded direct probe in `/tmp/graphrag-declared-mention-probe.json` used
that same original 185-character file and SHA256,
`b7563697240481461e143d34ccde4a5c78e4fd60b354110b2276d05b934706b2`.
With the v3 prompt it made exactly one provider call and zero database writes,
completed in 7.326 seconds, and produced a validated CANDIDATE result with
three entities, five mentions, four assertions and no findings. This is one
successful direct extraction observation, not a repeated quality benchmark,
an end-to-end latency comparison, or a completed construction/review/publication
workflow. The corrected full-service live workflow requires separate evidence
and is not claimed by this probe.

## Starter maintenance-entry simplification

Following the direct probe, review of the existing identity boundary found
that a resolution request only consumes identity-property facts bound to its
current mention revision. The original sample repeated the equipment name
three times, while the extracted EquipmentCode fact supported the first
mention. Later mentions therefore could return CONFLICT for missing identity
properties even though they shared the same model entity ref. This fail-closed
rule remains in force; neither a shared name/ref nor a fact on another mention
authorizes a link.

The current maintenance file is a natural single-equipment entry: one equipment
name followed by its code, power, component and risk in the same entry. It is
153 characters / 335 UTF-8 bytes, with SHA256
`84a62a94a14c7121bf86c5f2178ef499c132e12202a818a9c6e2f2ea83123ab8`.
The expert-source file, its evidence coordinates and the homonym example are
unchanged. This improves the introductory exercise; it does not establish
multi-mention identity propagation or retroactively change the first failure,
browser-download observation or direct probe. Those historical results refer
to the original 185-character sample. A deferred item tracks the evidence and
authorization design needed for identity facts across multiple mentions.

## Identity-query planning failure and correction

The first full-service use of the final 153-character report passed extraction
with three mentions and four assertions, but exposed a separate database
planning failure during Equipment identity matching. Initial Risk and
Component matching returned 200 in 49.741 and 23.924 seconds. The Equipment
request returned 504 at 105.020 seconds; one same-input read-only retry also
timed out. Candidate and publication state were unchanged.

Read-only `SHOW TRANSACTIONS` inspection found both identity-count queries in
`planning`, with zero page hits/faults; their observed elapsed times reached
4 minutes 34 seconds and 2 minutes 27 seconds. The API deadline had not stopped
the background planning work. Both were verified as this exercise's Beta
Equipment/EquipmentCode requests and explicitly terminated. The diagnostic
query SHA256 was
`4c95c1226a7dfc12e82c282113eed5117fa1344e92a15424bf82e81af20a027f`.
This is a failed workflow observation, not an acceptable cold-start pass.

The correction adds explicit `WITH DISTINCT` boundaries between authority,
source-snapshot and identity-property query stages. Independent review confirmed
that all original tenant/access, active-source, publication/T-Box and exact
evidence predicates remain. The global entity count is still unrestricted;
only the final evidence projection retains its existing limit of five. Count
and target fetch retain their activation-generation check in one transaction.

With the default planner and unchanged 1-CPU/1.5-GiB database, the first new
count and fetch statements took 1.818 and 2.013 seconds and returned the unique
`equipment-id:bc-p-101` authority with exact evidence. This direct read probe
wrote no database data. See [the query design record](../entity_resolution_query_planning.md)
and `/tmp/graphrag-identity-stage-probe.json`. The separately recorded final
service run below is required to establish the complete workflow after this
query change.

## Bounded validation feedback

A later fresh service run with the 153-character source and v3 prompt again
returned two `ENDPOINT_OUTSIDE_EVIDENCE` findings. It correctly created no
candidates. This is retained in
`/tmp/graphrag-industrial-demo-live-pre-feedback.json`; it is not relabelled as
a pass. Prompt clarification alone did not eliminate model variation.

The final Playground profile is
`industrial-property-graph-extraction:v4-validation-feedback`, with at most two
validation attempts per Chunk in a construction request. Only bounded JSON,
structural or evidence failures receive one corrective model call containing
the previous output and specific findings. The same strict validators apply;
no code patches evidence spans or invents extracted records. Oversized outputs
retain only checksum/size metadata and do not trigger correction. Provider
errors and timeouts remain explicit recoverable failures without automatic
provider retry.

Each actual call independently reserves the four-call request budget and must
fit its full 30-second provider timeout inside the unchanged 90-second workflow
deadline. Worst-case preflight therefore permits two LLM Chunks; SOURCE_ONLY
still permits four. SDK retry settings, model, output-token limit, database
resource cap and 105-second API timeout are unchanged. Explicit dependency
recovery is a new bounded request, not a promise of four total calls over the
job's entire lifetime.

Every attempt is persisted before correction or candidate writes in an
immutable artifact bound to the job, source/version/Chunk, exact ACL, T-Box and
model/request policy. Replays verify the full attempt chain, response hashes
and final outcome. The default one-attempt extractor preserves its existing
policy signature and cache behavior. Feedback artifacts are job-bound:
terminal rejection replays within the same operation/job; a new operation key
explicitly creates a new bounded job. API summaries describe the terminal run
only; previous interrupted-run audits remain on the server. The webpage shows
attempt number, disposition, findings and checksum, never raw model output.

Fourteen new focused unit tests cover evidence correction, final rejection,
JSON correction, timeout/oversize refusal, no pre-validation proposals,
worst-case call preflight, per-call deadline checks, interrupted recovery,
immutable-chain corruption and legacy signatures. The 105-test related unit
set passed. Two added real-Neo4j tests independently exercise audit persistence,
ACLs, completed replay corruption and dependency recovery using fake providers.

## Final live service workflow

The final service runs on `127.0.0.1:8002` with a fresh, owned
`sample-graphrag-playground-4c8e91d8` database. Before replacing the earlier
failed-test instance, a read-only ownership audit confirmed exactly ten
baseline documents plus the two owned Beta sources, one owned Beta T-Box and
seven owned expert record heads, with no unexpected data. Existing 8000/8001
services and databases were preserved. Alpha was left unwritten for the manual
walkthrough; the acceptance workflow used Beta Board.

Actual authenticated HTTP operations established the following behavior:

- SOURCE_ONLY produced one evidence Chunk and no model candidates. Explicit
  expert import and publication produced exactly seven AUTHORITATIVE records;
  active quality passed and the review queue remained empty.
- The maintenance report completed construction in 29.576 seconds with one
  model call: three entity mentions and four assertions, all exact-source
  spans verified. The terminal attempt summary matched the job-detail response.
  None of these candidates appeared in the active publication before review.
- Component alias matching returned the unique expert component in 5.099
  seconds. Equipment matching used typed `EquipmentCode=BC-P-101`, returning
  the unique expert equipment in 5.047 seconds. The new risk had NO_MATCH in
  1.117 seconds. These are complete HTTP observations after the query fix,
  distinct from the earlier direct count/fetch probe.
- After inspection, explicit linking approved the equipment/component mentions.
  Two model single-valued properties exactly duplicated expert values and were
  rejected with a duplicate-fact review note; the expert facts were retained.
  The risk mention and two relationships were approved. Publication activated
  five SECONDARY records alongside the original seven AUTHORITATIVE records.
  The 12-record active graph passed quality and saved an immutable audit.
- Filtered retrieval returned both source Chunks with the equipment, component,
  site and new risk plus its EXPOSED_TO relationship. AUTHORITATIVE_ONLY removed
  the secondary risk from the graph while retaining expert entities/properties
  and both text Chunks. All six demo citations across three retrieval requests
  matched exact source ranges, Chunk hashes and document-version hashes.
- Unfiltered retrieval independently ranked the two demo sources first and
  second. Three remaining context slots contained baseline financial-risk
  Chunks; this small example does not establish general precision or justify
  a scoring change. Filtered, authoritative-only and unfiltered requests took
  26.836, 15.393 and 20.548 seconds respectively. Alpha could not read the Beta
  construction job (404), and its filtered retrieval exposed no Beta Chunk.
- The homonym source actually exercised validation feedback: the first response
  failed evidence/temporal checks, and the second response passed unchanged
  strict validators. Both attempt hashes and the first findings remain in its
  API summary. The equipment mention carrying `BC-P-202` returned NO_MATCH;
  a second mention lacking its own identity fact returned CONFLICT. Neither
  linked to `BC-P-101`. This also demonstrates the documented multi-mention
  limitation without silently transferring another mention's identity facts.

The maintenance call passed on its first attempt; only the homonym call is
live-provider evidence of the corrective second call. Model counts and output
precision remain observations, not guarantees for arbitrary documents. No
provider error was retried automatically, no evidence was mechanically repaired,
and expert import was not substituted for model extraction.

The main HTTP harness completed 31 positive checks; the separate citation
check verified six exact citations. Cleanup first attempted to remove the whole
active manifest and correctly received 409: existing publication rules forbid
an empty knowledge set, and the active manifest remained unchanged. Cleanup
then removed only the five secondary records and retired the maintenance and
homonym sources through the governed API. The seven expert records and their
source remain in Beta; Alpha stays available for a clean manual exercise. The
walkthrough now explicitly preserves the expert baseline during retirement.
This expected guard rejection is retained in the log, not counted as a pass
for empty-publication support.

Raw local evidence: `/tmp/graphrag-industrial-demo-live.json`, the phase logs
`/tmp/graphrag-industrial-demo-release-*.log`, and
`/tmp/graphrag-industrial-demo-release-citations.json`.

## Final browser checks

Standalone Chromium against the final v4 service verified the advertised
2-attempt / 2-LLM-Chunk / 4-source-Chunk / 4-call / 90-second workflow /
30-second provider limits. All three current files downloaded with their exact
309/153/151-character contents and SHA256 values. Metadata/mode prefill and an
empty file picker behaved as documented. Screenshots of the execution-policy
notes and validation summaries were visually checked.

An isolated page used intercepted construction/job responses to verify a first
`ENDPOINT_OUTSIDE_EVIDENCE` rejection followed by `CANDIDATE`, safe summaries
on both upload and job detail, and identity-change rejection of stale results.
Injected raw-output fields at every level were absent from the rendered UI.
There were zero JavaScript errors and zero unexpected mutations. The simulated
construction response never reached the real service. The real browser checks
are preparation/read behavior; the live write lifecycle below uses authenticated
HTTP and is not claimed as browser-click evidence.

Evidence: `/tmp/graphrag-ui-qa.Rd97ZF/v4-feedback-report.json`,
`v4-mode-notes.png`, `v4-feedback-summary.png` and the replayable
`v4-feedback-qa.cjs` script in the same temporary directory.

## Scope and limits

The synthetic documents and instances are the user's agreed expert baseline
for this exercise; they do not claim external industrial certification.
SOURCE_ONLY skips the extraction LLM, but Embedding and database usage still
apply. SOURCE_ONLY retains the four-Chunk cap; LLM work now allows two
Chunks to reserve one validation correction per Chunk. Extraction timeouts,
model, credentials and exact-evidence validators are unchanged. Live provider observations are
workflow evidence; model output counts and precision are not guaranteed for
arbitrary industrial documents. The Playground still returns retrieved
context and graphs rather than final generated answers. The final readiness
check confirmed healthy Neo4j/embedding/index dependencies and no demo T-Box,
construction jobs or demo sources in Alpha; see
`/tmp/graphrag-industrial-demo-release-manual-readiness.json`.
