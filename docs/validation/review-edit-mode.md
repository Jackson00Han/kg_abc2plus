# Explicit review editing

Date: 2026-09-06. Scope: review-card interaction clarity.

The workbench previously displayed two checkboxes per card: batch selection
and opt-in submission of the JSON editor. Users understandably interpreted
both as selecting the fact for approval.

Cards now retain only the batch-selection checkbox. Default single-record
Approve, Reject and Quarantine actions submit status-only decisions. An
`编辑内容` button opens the disabled-by-default, initially hidden JSON editor.
While editing, the approval action explicitly reads `保存修改并批准`; the
toggle becomes `取消编辑`, and Reject/Quarantine are hidden. Cancel restores
the original generated JSON without changing batch selection or sending a
request. Editing preserves the strict raw-only input mapping from the previous
contract correction.

Batch actions refuse to submit when any selected record is still being edited.
This prevents a batch action from silently dropping a draft or implicitly
saving it. Only an explicit single-record save-and-approve action submits an
edit. Invalid JSON leaves the editor open and sends no review request.
Approval remains separate from publication; fact deduplication is unchanged.

## Validation

The executable UI-to-HTTP contract test exercises entering/canceling edit
mode, visibility and button labels, invalid JSON, the batch draft guard,
changed-confidence submission for entity/property/relationship edits, and
status-only batch approval. It sends the generated requests through the
authenticated strict FastAPI contract and retains readonly-field rejection
and source-unit/evidence checks.

Passed: 707 unit tests (152.394 seconds), 15 HTTP E2E tests (1.510 seconds),
33 security tests (2.769 seconds), Python compilation and inline JavaScript
syntax checks, whitespace checks and the staged-file secret scan.

Repeatable checks:

```sh
uv run --locked python -m unittest discover -s tests/unit -t . -q
uv run --locked python -m unittest discover -s tests/e2e -t . -q
uv run --locked python -m unittest discover -s tests/security -t . -q
uv run --locked python -m compileall -q src tests scripts
git diff --check
```

An isolated Chromium session loaded the actual restarted `8002` page with two
mock review cards. It verified the single checkbox, initially hidden editor,
button visibility, plain single approval, cancel restoration with selection
preserved, the batch draft guard, edited approval, and two-record batch
approval. All three captured review requests passed `ReviewBatchRequest`.
Review POSTs were intercepted; no knowledge records were written. There were
no JavaScript page errors. Screenshots were inspected for the default and
editing states. Local evidence is in `/tmp/graphrag-review-edit-mode-qa/`.

A separate authenticated read verified that the real Tenant Alpha review
queue is empty, `8002` serves the exact current HTML, and `8003` refuses
connections. The first 15-second queue probe during cold startup timed out;
a subsequent bounded probe succeeded after startup/browser activity settled.
This does not establish a new latency baseline.

The test inventory is unchanged; the existing contract test is strengthened.
This frontend maintenance does not change persistence or count as a new
Stage 8/9 validation run.

## Service lifecycle and manual check

At the user's request, the temporary `8003` proxy was stopped and the `8002`
Playground was restarted through its normal disposable-database launcher.
The previous `8002` demo database was removed and a fresh baseline initialized;
the walkthrough can be followed from the beginning. The older, unrelated
`17692`/`17693` database containers were not changed.

Use `http://127.0.0.1:8002/playground` and follow
`docs/industrial_demo_walkthrough.md`. After extracting candidates:

1. Check that each review card has one batch-selection checkbox and its JSON
   editor is initially hidden.
2. Approve a correct individual record directly, without selecting it.
3. On another record, open `编辑内容`, make a draft change, and cancel. Confirm
   the original value returns and no review was submitted.
4. Open editing again, make a supported correction, and use
   `保存修改并批准` to submit it for approval.
5. Select multiple records for batch review. If a selected record is being
   edited, finish or cancel that edit before the batch action.
