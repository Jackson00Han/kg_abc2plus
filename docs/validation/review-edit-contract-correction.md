# Playground review edit contract correction

Date: 2026-09-06. Scope: default review editor serialization.

## Finding and correction

Selecting the JSON edit checkbox and approving the unmodified generated
`RatedPower` edit returned HTTP 422. The page copied `EntityIdentityResponse`
objects into the request, including their server-owned `entity_id`. The strict
`KnowledgeEntityInput` contract intentionally excludes that field. The same
defect affected entity mention edits and both endpoints of relationship edits.

`reviewEdit` now explicitly maps entity type, canonical key, canonical name
and aliases into each input identity. It leaves the queue response untouched.
Literal edits continue to submit raw source values, units and times; the
server continues to own normalized semantics and entity IDs. Explicit unknown
fields remain invalid. Status-only reviews, entity linking, evidence, revision
checks and publication behavior retain their existing contracts. Approval does
not perform semantic fact deduplication.

## Verification

The existing raw-only import/review HTTP test now executes the actual page's
`reviewEdit` and `submitReviews` functions in Node, then sends their requests
through the authenticated FastAPI route with a typed test backend. Before the
fix, generated edits returned 422 while status-only requests returned 200.
After the fix both return 200. The expanded test covers mentions, literal
assertions, relationship endpoints and relationship properties, preserves raw
units/times and evidence, verifies no queue mutation, and checks that manually
supplied `entity_id` fields still return 422.

Passed checks:

- 707 unit tests, 111.929 seconds.
- 15 HTTP E2E tests, 1.010 seconds.
- 33 security tests, 1.907 seconds.
- Python compilation, complete inline JavaScript syntax check, whitespace
  checks and a secret scan of the staged files.

Repeatable commands:

```sh
uv run --locked python -m unittest discover -s tests/unit -t . -q
uv run --locked python -m unittest discover -s tests/e2e -t . -q
uv run --locked python -m unittest discover -s tests/security -t . -q
uv run --locked python -m compileall -q src tests scripts
git diff --check
```

This is a frontend correction with HTTP boundary coverage, not a new full
Stage 8/9 validation run. No Neo4j persistence logic changed; the disposable
database suite was not rerun. The existing test ID was expanded, so the test
inventory did not change.

## Current local service and browser check

The running Playground reads its HTML once at startup. Its disposable database
launcher stops and removes the database when restarted. To preserve the user's
existing construction progress, a temporary loopback preview at
`http://127.0.0.1:8003/playground` serves the corrected HTML and forwards API
requests to the existing `8002` backend. It uses the same authentication and
data; the original `8002` process still serves its startup HTML. This preview
depends on that original backend and is not a persistent deployment. Future
normal launches load the committed fix directly.

An isolated Chromium session selected `Tenant Alpha · Finance + Legal` and
opened the user's existing `RatedPower` record
`582f46ad-1a4a-564e-8953-33c3f9fe04d7`. Checking JSON editing and clicking Approve
generated the correct `37.5 kW` raw-only payload without an entity ID. The
browser intercepted that POST before it reached the backend; its captured body
passed the real `ReviewBatchRequest` schema. A subsequent queue read retained
revision 1, and there were no JavaScript page errors. No user review, entity
link or publication was executed by this check.

Local evidence: `/tmp/graphrag-review-fix-preview-20260906/browser-report.json`,
`intercepted-review.json` and `review-fixed.png`. The temporary preview process
and its script live outside the repository.

Manual verification: open the preview, choose the same governance identity,
enter Knowledge Construction and refresh the review queue. The generated
editable entity/subject/object objects should contain no `entity_id`. Review
the source evidence and retry the intended action. Approval remains separate
from publication and does not automatically resolve duplicate facts.
