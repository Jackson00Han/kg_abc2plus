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
