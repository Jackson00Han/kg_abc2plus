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
entity-edit revision dependencies and the independent industrial workbench's
locked upload policy remain as documented in the operation guides.
