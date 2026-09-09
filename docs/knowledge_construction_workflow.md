# Knowledge construction workflow

## Current implementation work

The owner approved the normal-use workflow on 2026-09-08. This replaces the
source-only/template-driven authority exercise; it does not change Neo4j or
introduce a second database. Existing published revisions remain immutable.

The intended user flow is: save and activate the ontology, upload authoritative
documents and extract, edit/confirm the instances, then publish. Business-document
construction remains a separate secondary-knowledge flow. The ontology is the
actual application ontology, not a simulated schema; technical intermediate
states must not become a required user-facing draft workflow.

## Fixed source authority

| Origin | Authority | Meaning |
| --- | --- | --- |
| `AUTHORITATIVE_EXTRACTED` | `AUTHORITATIVE` | Extracted in the explicit authoritative-document construction flow |
| `LLM_EXTRACTED` | `SECONDARY` | Extracted from business documents |
| `HUMAN_SUPPLEMENT` | `SECONDARY` | User-entered facts beyond document evidence |
| Existing expert/rule/fixture origins | Unchanged | Compatibility with immutable historical records |

Review changes lifecycle state, never authority. Authoritative candidates have
that source grade immediately, but only explicitly published revisions belong
to the active graph. Business review/editing cannot promote a record. Manual
corrections supported by the cited document retain the document origin; a claim
outside that evidence needs an explicit human-source record, not a fabricated
quote or automatic fallback after failed extraction.

Authoritative construction requires both `knowledge:construct` and the existing
`knowledge:import` capability. The API accepts a closed `knowledge_scope` with
`BUSINESS` as its backward-compatible default. Source ACL checks still apply
before work begins. An authority-specific extraction profile separates model
artifacts, record IDs and request fingerprints from business construction.
Changing scope cannot reuse a completed operation as if it were the same input.

No new personnel directory, role-management UI or responsibility workflow is
part of this change. Existing authentication and protected-content boundaries
remain active.

## Implementation sequence

1. Fixed source authority and compatible readers, with focused workflow and
   real-Neo4j persistence/replay/ACL checks.
2. Normal ontology and document workflow, instance editing and explicit human
   additions, with bounded API and browser contract checks.
3. Source-aware publication, retrieval and citations, with end-to-end source
   and authorization checks.
4. Existing-data compatibility, complete regression, browser acceptance and
   versioned validation evidence.

Validation results and any remaining limitations are recorded here as each step
completes. This maintenance does not renew historical Stage 9 qualification.

## Step 1 validation

Fixed source-authority rules and compatible API/graph readers are implemented.
Checks passed: 1,013 unit, 16 HTTP E2E, 54 security, two regression and ten
real-Neo4j construction tests. The Neo4j run used the unchanged `dev-mini`
1 CPU / 1.5 GiB cap, a disposable database and deterministic providers; it
included authoritative candidate persistence, ACL isolation, exact replay,
mode-conflict rejection and the existing recovery checks. Focused unit checks
also cover unchanged business authority after review and authoritative
low-confidence quarantine.

Repeat the unit/E2E/security/regression commands in README. For the bounded
construction integration subset, run the existing Stage 8 disposable runner
with its test pattern set to `test_construction_workflow_neo4j.py`; the complete
unchanged runner includes it. Local results are
`/tmp/graphrag-authority-{unit,integration}.json` and corresponding logs;
these are temporary artifacts, not durable qualification reports. The final
complete regression and baseline refresh remain Step 4 work.


## Steps 2–4: completed workflow and validation

The normal page now saves and activates the application ontology in one
transaction, extracts authoritative documents, offers instance confirmation and
explicit human additions, and publishes through the existing governed manifest.
The shared instance queue labels each record's authority and origin. Business
construction never upgrades after review. The former expert-template import
panel is hidden compatibility markup; the normal API adapter refuses legacy
expert creation. Historical import fixtures explicitly opt into the compatibility
adapter, and existing published expert data remains unchanged.

Human input uses `MANUAL` construction with typed entity, property or relationship
fields. The server renders the actual submitted record, pins its immutable
version and exact range, validates it against the ontology, and stores it with
`HUMAN_SUPPLEMENT / SECONDARY`. It performs embedding but no extraction model
call. `urn:graphrag:human:` is reserved; document uploads cannot impersonate it,
and documentary origins cannot use human records as documentary evidence.
Citation responses explicitly report `source_kind=DOCUMENT|HUMAN_RECORD`.

Steps 2 and 3 are delivered together at the publication boundary: exposing the
human-entry UI requires its retrieval gate. Every recall, graph-degree/expansion,
candidate ranking, adjacency and final hydration query excludes human source
text unless all its current records are in the active publication. The final
corpus/publication consistency check remains in place. Partial publication,
unpublished edits and unauthorized audiences cannot expose the complete human
statement. No database migration or existing-data relabelling was performed.

Validation on 2026-09-08 passed 1,023 unit, 169 disposable-Neo4j integration,
16 HTTP E2E, 54 security and two regression tests, with no skips. Additional
checks passed for security-suite completeness, reproducible development corpus
and gold data, acceptance contracts, knowledge quality, changed-file Python ASTs,
inline JavaScript syntax, whitespace and secret patterns. The Neo4j suite ran
under the unchanged 1 CPU / 1.5 GiB dev-mini cap with deterministic providers.
Atomic ontology activation includes rollback/replay coverage. Manual provenance
includes create/replay, review/publish, partial-publication exclusion and ACL
checks. Executed page JavaScript checks cover actual flow ordering and shared
controls, authority scope submission, manual retry identity, and stale identity
responses. A first development run found a frozen-response initialization bug
and obsolete UI expectations; those were corrected before the complete passing
run. No failed or skipped tests were excluded from the final suite reports.

Baseline 1.11.0 retains every 1.10.0 test ID and adds 23 unit plus four integration
checks, including earlier maintenance not yet present in 1.10.0. Graph and
retrieval cases, quality metrics and diagnostics are unchanged. All 51 answer
case digests exactly match 1.10.0 after restoring only `prompt_version` to
`grounded-answer-v1.3.0`; current version 1.4.0 distinguishes human input in the
answer prompt. The sole configuration difference is that prompt version.
The operational file remains a deterministic, non-performance fixture, not a
new latency or cost measurement. Final evaluation semantic digest:
`ffd9bed871c9c49ef4c09e401e96d433537c3ae5afda7ca6e75bb022cf176e19`.

Local full-run artifacts are `/tmp/graphrag-workflow-validation`,
`/tmp/graphrag-workflow-observations-final`, and the corresponding
`/tmp/graphrag-workflow-*-final.json` suite reports. The checked-in baseline and
this record retain the reviewed outcome; temporary files are not production
qualification evidence. The existing complete two-run Stage 8 command in README
can reproduce the development gate. This maintenance run does not renew Stage 9.

The live Chinese workbench at port 8000 was restarted with
`--reuse-existing-corpus --skip-provider-warmup`. Authenticated ontology and
review-queue reads and the new OpenAPI contracts passed; the database stayed at
91 nodes across restart. The initial cold review query took about 20 seconds;
a subsequent read took 0.59 seconds. No fixture reset, source upload, knowledge
publication or provider warmup was performed on the user's database. The
independent industrial service at port 8002 was left running.

Browser visual acceptance remains unverified because no browser surface was
available to the computer-use tool. HTTP and executed-JavaScript checks passed;
these do not substitute for a visual check. Existing constraints on ordinary
entity-edit revision dependencies applied to that release; the dependency
handling is updated below. The independent industrial workbench's locked
upload policy remains as documented in the operation guides.

Navigation follow-up (2026-09-08): the shared review completion button had
hardcoded the business flow, moving authoritative step 03 to business step 07.
It now retains the current construction flow: authoritative 03 → 04 and
business 06 → 07. The existing executable workflow check now clicks the actual
bound handler in both flows and verifies the publication container, number,
title, scrolling, preserved selection and absence of API writes. It reproduced
the baseline-to-business failure before the fix. All 102 `test_playground*.py`
unit checks passed afterwards (`uv run --locked python -m unittest discover
-s tests/unit -t . -p 'test_playground*.py' -q`), as did JavaScript syntax,
Python AST, whitespace and changed-file secret checks. No test IDs or backend
contracts changed. The port 8000 UI service was restarted with existing-corpus
reuse because its HTML is cached at startup; an HTTP read confirmed the fixed
handler and the existing graph remained at 109 nodes across restart. This is
executed-JavaScript and HTTP evidence; no browser visual check was performed.

## Standardized review and complete publication preview

The next maintenance change keeps joint extraction and separates review into
entity identity confirmation and fact review. It does not run a second model
extraction after entity confirmation. The fact phase has property and
relationship tabs, with neither tab requiring completion of the other.

Each entity mention has three aligned disclosures: source text, identity
matching/disambiguation, and **标准化实体**. Matching suggestions and targets are
inside one disclosure. Confirming an existing identity reuses its application
ID and canonical name, retains source evidence and accumulates accepted names
as aliases. It does not approve dependent properties or relationships.
Aliases are stored on governed source revisions, and the publication view
aggregates them across mentions. The existing canonical Entity node is not
overwritten with a global alias union. Authoritative alias matching reads only
visible authoritative mentions, preserving authority and access boundaries.

The editable JSON is deliberately limited to business fields:

| Editor | Editable fields | System-maintained fields |
| --- | --- | --- |
| 标准化实体 | `entity_type`, `standard_name`, `aliases` | `entity_id`, `evidence_ids` |
| 标准化属性 | confirmed `entity_id`, `property_name`, raw `value`, `unit`, validity/observation times | `property_id`, `evidence_ids` |
| 标准化关系 | confirmed source/target IDs, `relationship_type`, existing relationship-property names/values/units/times | `relationship_id`, `evidence_ids` |

Endpoint selectors are restricted to available confirmed mentions in the
same source chunk; the server still validates their exact revision and evidence
dependencies. IDs and evidence references cannot be forged through the editor.
The original quote remains in the source disclosure. Confidence, source grade,
revision counters and extraction metadata are not editable business inputs.
Values and units are normalized by the backend from edited raw inputs.
An empty alias or relationship-property list remains empty unless supported
content is explicitly supplied; the UI does not invent missing facts.

Saving an edit requires a new validation and review. Returning an approved
entity to editing atomically creates a new mention revision, rebinds pending
dependent facts and withdraws their prior approvals. Their source grades and
evidence remain unchanged. Approved records can be returned to step 03 from
the publication candidate list. Historical published revisions remain immutable.
Same-value facts from a second source can be accepted as additional evidence;
their source records and grades remain separate rather than replacing the
earlier source. Conflicting values still require the existing assessment and
ontology cardinality checks.

Step 04 first calls `POST /v1/knowledge/publications:preview`. It displays
CREATE/UPDATE/REMOVE cards for entities, properties and relationships, including
the previous value on updates, and a complete read-only JSON projection:

- `entity_changes`, `property_changes`, `relationship_changes`: changes against
  the active publication, with before/after values.
- `entities_after`: final entity identities, combined aliases, source grades
  and mention/evidence references.
- `records_after`: every immutable governed record in the resulting manifest,
  including unchanged records, full typed literals and exact endpoint revisions.
- `evidence`: complete source document/version/chunk/range/quote and access
  metadata for each final record, plus source origin and grade.
- publication, ontology and base-publication IDs, selected revisions,
  removals/replacements, `manifest_hash` and `preview_hash`.

This is the application's governed publication format, not a claim that Neo4j
defines a universal JSON import schema. The existing Neo4j materializer builds
navigation nodes/edges from these records; source chunks remain answer evidence.
Evidence IDs in this projection are the immutable record revision IDs. Entity
grade lists summarize contributing sources and do not promote secondary facts.

Preview runs the actual publication transaction through validation and graph
materialization, then deliberately rolls it back before returning the result.
It commits no publication, revision, state counter or navigation changes. It
uses write transaction locks temporarily, so its cost is comparable to
publication rather than a lightweight list query. The page sends the resulting
`expected_preview_hash` on publication. A changed selection, review, active
publication or manifest invalidates the preview; a mismatching hash rejects
the transaction. The server regenerates the final records, never accepts
client-edited preview JSON as graph writes. Older API callers can still omit
the hash for compatibility; the normal page requires it.

These controls improve traceability and prevent stale/unreviewed publication;
they do not prove semantic entailment or guarantee extraction precision.
Reviewers must check names, identity, values, units, direction and source text.
Changes beyond documentary evidence use the explicit human-source workflow
and remain secondary. Existing canonical-identity conflicts remain blocked;
this change does not introduce a global rename or unrestricted graph editor.

### Validation for standardized review/publication (2026-09-09)

The final suites passed without skips: 1,027 unit, 173 disposable-Neo4j
integration, 17 HTTP E2E, 54 security and two regression tests (1,273 total).
The complete integration run used deterministic providers and the unchanged
1 CPU / 1.5 GiB dev-mini cap. It checks complete database rollback after
preview, deterministic repeat previews, exact manifest equality on publication,
hash mismatch rejection, missing dependencies, source access, retained records,
removal, and invalidation of dependent approvals when an entity is reopened.
Executed page JavaScript and HTTP checks cover protected editor fields, raw
literal conversion, the two fact tabs, preview-required publication, changed
selections, duplicate clicks and the preview API contract.

Baseline 1.12.0 retains every 1.11.0 test ID and adds four unit, four Neo4j and
one HTTP E2E checks. All case digests, contract metrics, quality diagnostics
and configuration identities are exactly unchanged. Semantic digest:
`e524c30d084437a06f688c57893ec0d59b361ad47624bda2d709d387ba785311`.
The operational observations remain deterministic fixtures; this change makes
no new performance or production-scale qualification claim.

Reproducible commands remain in README. This run's full suite receipts,
retrieval/answer/conflict observations and unified report are under
`/tmp/standardized-validation`. The fixed knowledge-quality gate, required
security-test manifest, corpus/gold rebuild checks and acceptance contracts
also pass. An initial unit/HTTP run exposed outdated editor and publication
expectations; those were corrected and their complete suites rerun. No tests
were removed or skipped to produce the final reports.

The port 8000 workbench was restarted with existing-corpus reuse and provider
warmup disabled. Live HTML/OpenAPI and authenticated review/candidate reads
passed. The graph had 97 nodes before and after restart; no source upload,
review mutation or publication was performed against the user's database.
The independent industrial workbench on port 8002 remains untouched.
Browser visual acceptance is still unverified because no browser surface or
local browser test package was available. HTTP and executed-JavaScript checks
do not substitute for that visual check.

## Follow-up: confirmed identities and source context

The first implementation of standardized review still matched only published
authority and displayed only the exact mention quote. This left a reviewer
unable to link later mentions to an identity just confirmed in step 03 or to
judge a four-character equipment name in context. This follow-up changes those
two review surfaces; extraction, fact approval and publication stay separate.

The automatic authoritative matcher is unchanged. Missing identity properties
are displayed as insufficient evidence, distinct from conflicting known values.
An additional manual selector lists visible, current `APPROVED` mentions and
mentions in the active publication, restricted to the same ontology and entity
type. It groups results by entity ID, returns at most 20 entities, reports
truncation and supports name/alias/canonical-key/ID search. Unpublished targets
are explicitly labelled. The reviewer can inspect both source contexts before
choosing a target and recording the reason. This action is never an automatic
same-name merge or an authority promotion.

Manual confirmation includes the selected mention record ID and expected
revision. The shared corpus and record locks protect revalidation within
the existing resolution transaction: target status, type, ontology, exact source,
ACL and revision must still be valid. Known conflicting identity-property values
block linking; absent values alone do not. Identity checks include the visible
confirmed sources of a deduplicated entity, so changing its representative
mention cannot hide a known conflicting value. Linking retains the candidate's own
evidence and grade, appends a mention revision and rebinds dependent facts without
approving them. After identity review the page refreshes matching so newly
confirmed targets appear for the remaining mentions.

`POST /v1/knowledge/review-evidence` is a `knowledge:review` read operation.
It accepts a record ID, expected revision, view and bounded document-page offset;
it never accepts a replacement source range. The server resolves the active
source version and verifies the exact quote against both chunk and document.
The page initially shows the containing paragraph within a bounded window,
highlights the mention and shows the document title, version and character
positions. Reviewers can expand up to 2,000 characters on either side or read
the pinned normalized document in 8,000-character pages. Very long paragraphs
remain bounded; displayed character ranges make the window explicit. Highlight
positions use Unicode code points, including supplementary characters.

Document expansion requires access to every chunk of the pinned version. If
the document includes an inaccessible chunk, the normal context falls back to
the accessible source chunk and full-document access is refused. Source reads
are lazy and escaped for HTML; late responses after identity changes or removal
of the card are discarded. Stored quotes and evidence offsets are unchanged.


Live read-only verification on 2026-09-09 restarted only the port-8000 Python
workbench with `--reuse-existing-corpus --skip-provider-warmup`; the existing
Neo4j container was retained. Both remaining candidate mentions returned one
selectable confirmed target. Their four-character quotes expanded to containing
paragraphs of 16 and 22 characters, and both surrounding/full-document reads
returned the pinned 308-character normalized source. Every returned quote matched
its exact source range. The graph before and after restart plus authenticated
reads contained 104 nodes and 157 relationships with the same full-property
fingerprint (`6dfdcfd7b1755ddaac878799031fe8ce6929d04d0c075a2d9dfb597ccef96d21`).
No review decision, publication, reset or provider call was used for this check.
The port-8002 workbench was left untouched. Browser visual acceptance remains
unverified; the checks execute page JavaScript and authenticated HTTP operations.

The focused disposable-Neo4j review module passed all 20 tests, including
continuous three-source identity linking, identity conflicts from another
confirmed source, stale target revision rejection without residual writes,
unchanged source grade, unapproved dependent facts, pinned source context and
restricted neighboring-chunk denial. Earlier attempts exposed a query-parameter
name collision and two test setup assumptions (a fixed representative source and
an assertions-only batch); these were corrected before the successful rerun.
An earlier complete run was interrupted after Bolt connection timeouts. Its log
is retained locally at `/tmp/review-context-integration-full.log`; an orphaned
test container from that run could not be removed because Docker did not report
an exit event. The Docker daemon was not restarted, to preserve the user's
running databases. Subsequent checks use fresh, separately owned containers with
the unchanged 1-CPU/1.5-GiB resource cap.


Final validation for this follow-up: **1,281 tests passed without skips** —
1,032 unit, 18 HTTP E2E, 54 security, 2 regression and 175 disposable-Neo4j
integration tests. The final complete Neo4j run passed in 1,374.682 seconds and
its owned container was removed successfully. The updated page's manual apply
request and stale-context handling were also executed in the Node.js harness.
Packaging, Python compilation, JavaScript syntax and `git diff --check` passed.

Baseline **1.13.0** retains every 1.12.0 test ID and adds exactly five unit,
one HTTP E2E and two Neo4j integration tests. `case_digests`, `contract_metrics`,
`diagnostics` and `identities` compare exactly equal to 1.12.0. The unified report
passes against the updated baseline with semantic digest
`0f8f32b0795c8707a7868f0f799511539045f9d874d31801e984419d9621cea3`.
The locked knowledge-quality gate also passes. Operational figures remain
non-qualifying deterministic fixtures; this is a `dev-mini` maintenance check,
not a new production performance validation.

Local receipts, observations and the unified report are in
`/tmp/review-context-validation`; the baseline comparison is in
`/tmp/review-context-baseline-comparison.json`. Reproduce the integration suite
with `sh scripts/run_stage8_neo4j_tests.sh SUITE_RESULT_PATH OBSERVATION_DIR` and
the UI/adapter checks with
`.venv/bin/python -m unittest tests.unit.test_review_context tests.unit.test_api_entity_resolution`.
The normal Stage 8 evaluation command consumes all five suite receipts and the
locked knowledge-quality report; no acceptance threshold was relaxed.

## Publication entity grouping and selection integrity (2026-09-09)

Step 04 now groups the current candidate batch by canonical `entity_id`, matching
step 03 and the existing publication projection. Each entity card contains
separate expandable source-mention, property and outgoing-relationship sections.
Names do not determine grouping: homonyms with different IDs remain separate.
Each source/fact retains its record ID, effective revision, provenance, exact
source context and individual return-to-review action. Group and leaf checkboxes
select records, not multiple competing versions of one entity. The full
publication preview remains the authoritative before/after write plan; grouping
does not change the Neo4j schema, source grades or publication transaction.

Checkboxes and the advanced revision-ID input now share the same selection.
Unchecking a record removes it from the submitted request; reviewing another
record does not reselect previously unchecked records. Selection changes preserve
expanded source context and keyboard focus. Returning a source to review removes
its old revision and any invalidated dependent-fact revisions from selection.
Unsaved step-03 edits must be saved or cancelled before returning another record.
Review and publication mutations block one another until they finish. Superseded candidate
fetches and responses from a previous login identity cannot restore stale cards,
selections or busy state. Changed content/selection invalidates the saved preview;
the server still validates the exact preview hash at publication.

The candidate endpoint remains bounded to 100 records. Cards explicitly describe
counts as belonging to the current batch; a large entity can span batches.
Grouping does not automatically select unseen records or claim a complete global
source count. The final server preview validates required identity dependencies
and carries the effective retained records into the complete publication plan.

Live read-only acceptance used the current 11 candidate records: three entity
cards contain seven mentions, two properties and two relationships. The pump
card contains three mentions, two properties and two outgoing relationships;
the site and seal cards each contain two mentions. All 11 source-context reads
matched the stored exact quote. Executing the page's grouping and selection
functions with this real API payload preserved every record once and selected/
deselected the intended group. Restarting only the port-8000 Python workbench
retained the same 129 nodes, 235 relationships and full-property fingerprint
`6318912a048e0aa6ef7c229102f053a774c0b106eb2c2fdcf392939de0f50764`.
No review, publication, reset or provider call changed the user's graph.
Browser control surfaces were unavailable: functional acceptance executes the
page JavaScript, authenticated HTTP endpoints and disposable-Neo4j transactions;
a visual browser-click acceptance pass is not claimed.

The focused disposable-Neo4j review/publication module passed all 21 tests.
The added lifecycle case publishes three independently traced source records
sharing one entity, including a source returned to review and approved again.
It verifies one materialized entity, all three evidence references, one effective
revision per record and rejection of historical/current revisions selected
together without residual writes.


Final validation: **1,286 tests passed without skips** — 1,036 unit,
18 HTTP E2E, 54 security, 2 regression and 176 disposable-Neo4j integration
checks. JavaScript syntax, Python compilation, packaging and `git diff --check`
also passed. Baseline **1.14.0** preserves every 1.13.0 test ID and adds exactly
four unit tests and one integration test. Case digests, contract metrics,
diagnostics and configuration identities compare exactly equal. The locked
knowledge-quality gate and unified report pass, with semantic digest
`54408bfc553bba2c79b493dd66b1c52f82b00c7a96d605e0f541e0e965254f6a`. These are `dev-mini` maintenance results;
operational fixtures do not qualify as new production performance evidence.

Receipts and the report are in `/tmp/publication-groups-validation`, the baseline
comparison in `/tmp/publication-groups-baseline-comparison.json`, and live
read-only results in `/tmp/publication-groups-live-result.json`. Reproduce the
focused UI checks with `.venv/bin/python -m unittest
tests.unit.test_publication_groups tests.unit.test_standardized_publication
tests.unit.test_review_context`. Run the complete Neo4j suite with
`sh scripts/run_stage8_neo4j_tests.sh SUITE_RESULT_PATH OBSERVATION_DIR`, then use
the existing Stage 8 evaluation workflow with all five suite receipts.

### Complete instance JSON disclosure

The publication preview's **查看完整实例 JSON** disclosure now shows the complete
post-publication entity/property/relationship snapshot with exact evidence,
including retained records. It is a read-only business projection, not the Neo4j
write payload. The original change manifest remains in the audit disclosure.
See [implementation and validation](validation/instance-json-preview.md).
