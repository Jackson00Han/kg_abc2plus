# I5 complete regression and baseline maintenance

This report covers complete Stage 8 regression after industrial milestone I4,
commit `74d1c4dc3c6d6afdf6c4067a8a79688e9c9806c2`. It is development-scale
validation under the unchanged `dev-mini` limits. It does not qualify a new
production deployment or replace the earlier Stage 9 qualification evidence.

Status: complete. Two full executions each passed all 1237 tests, and their
deterministic reports match the reviewed baseline `1.10.0`. The original I4
capture remains historical evidence only.

## Preserved evidence

The original baseline is version `1.9.0`, last updated in commit
`260307d3efa8abf178a420f5a10cba77db31d644`. Its semantic digest is
`8c03cd166a6fc218c4bcd10c19bf4d6ea59152e8302bea72d4be2941f53af094`.
It records 763 unit, 16 HTTP E2E, 33 security, two regression and 138 disposable
Neo4j tests: 952 total. Its historical report remains unchanged in
[construction-ingestion-failure.md](construction-ingestion-failure.md).

The first I5 capture retains that baseline and its matching version constant.
This ordering matters: the production-report unit test reads the checked-in
baseline and requires its version to match the evaluator constant. Changing
only the constant before candidate capture would fail that existing test.

The candidate must preserve every old test ID and all old case digests,
contract metrics, diagnostics and identity domains. Only reviewed additional
test IDs may change its deterministic projection. Baseline version and review
rationale are metadata outside that projection. A successful candidate capture
does not by itself approve a baseline: candidate mode does not compare against
the old baseline, and a failing evaluation can still write a candidate file.

## Capture and verification procedure

All artifacts are external to Git, under `/tmp/graphrag-industrial-i5`.
`capture-input-pins.json` fixes SHA-256 values for 425 tracked implementation,
test, script, data, evaluation and configuration files. The original baseline
is separately retained as `baseline-1.9.0.original.json`.

The environment uses Python `3.12.12`, uv `0.9.7` and Node `v23.11.0`.
The complete runner creates and removes its own loopback-only Neo4j container;
it does not reuse the retained Playground database. Database limits remain one
CPU, 1536 MiB total memory, a 512 MiB maximum heap and a 128 MiB page cache.

First complete capture:

```sh
./scripts/run_stage8_validation.sh --repeat 1 \
  --baseline-candidate /tmp/graphrag-industrial-i5/baseline-candidate.json \
  --output-dir /tmp/graphrag-industrial-i5/capture
```

The command exited zero. All 1188 tests passed, with no failures, errors, skips,
expected failures or unexpected successes:

| Suite | Passed |
| --- | ---: |
| Unit | 955 |
| HTTP E2E | 16 |
| Security | 54 |
| Regression | 2 |
| Disposable Neo4j | 161 |

The Neo4j suite took 1354.896 seconds. The locked extraction-quality gate,
legacy corpus/gold rebuilds, acceptance contract, dependency lock, package
build, bytecode compilation, shell syntax and diff checks passed. The separate
industrial contract, corpus, composed model and frozen-gold validators also
passed. The report semantic digest is
`ab15d2f0ac6e13b876791c4c5fce381fd94a7187be2062af1e55a65d6f21766e`.

All 425 initially pinned input files remained byte-identical at completion.
The runner-owned `sample-graphrag-stage8-neo4j-52916` container was removed.
`capture-completion.json` records the successful exit, counts, durations and
artifact SHA-256 values. This is one complete execution of I4's fixed code.
Its passing report is not approval of the candidate baseline and does not
resolve the live-upload findings. Any subsequent implementation correction
requires a separately pinned complete capture; results from different code
states must not be combined.

After strict review, baseline metadata and its evaluator version constant are
updated together. The first observation set is then checked against the
reviewed baseline by rebuilding its report. That operation is a report replay,
not another execution of the test suites. A second complete runner execution
uses a fresh output directory and a new disposable database; its report must
match the first capture's deterministic projection.

## Corrections and a separately pinned I5 capture

The live-upload walkthrough exposed failures beyond the passing I4 suite.
Corrections preserve strict evidence checks while improving the extraction
contract and its feedback, handling Chinese STRING values consistently across
extraction and downstream governance, and recovering an interrupted terminal
validation run from its unchanged saved responses. Recovery revalidates scope,
artifact identity, lineage and the actual response; it does not trust the saved
success label or call the model again for a complete recoverable run.

Focused disposable-database captures are retained separately under
`/tmp/graphrag-industrial-i5-fix-db*`. Their failures were not relabelled as
passes. They identified a JSON-format-dependent audit lookup, an overly complex
cold query, and a missing current-job authorization check before source
preparation. The final reader validates a bounded candidate collection without
JSON substring matching and checks current source authorization before and
after reading. Job authorization now precedes the ingestion pipeline. Its
legacy compatibility scan is limited to 256 tenant/profile artifacts, 32
matching attempts, 8 MiB per artifact and 32 MiB total; exceeding a bound fails
closed and does not fall back to a new model call. A future indexed migration
is required for recovery profiles that exceed that legacy scan limit.

One new recovery fixture initially exhausted its test-only 30-second total
budget during cold source preparation. Only that helper was changed to match
the existing industrial online 90-second envelope. The runtime was unchanged;
the 15-second database bound, one-second scripted model bound, existing
30-second feedback fixtures and deadline tests remain intact. Failed captures
and their reasons remain available in each external completion record.

The final focused run executed both complete construction-workflow and graph
projection modules: 14 passed in 275.471 seconds, with no failure, error or skip.
It includes Chinese typed-literal recovery, deliberately corrupted and fully
re-signed audit chains, current document/chunk/job ACL revocation and recovery,
review/publication and exact-source graph projection. All 429 pinned inputs
remained unchanged and its owned database was removed. Evidence is in
`/tmp/graphrag-industrial-i5-fix-db5/completion.json`; its suite SHA-256 is
`2212f935e0f9583276abd747c1024c5a4cc1bc8c1ef3546cf25eb3903c6f75bd`.

The corrected complete capture runs from the isolated fix worktree with its own
locked virtual environment. Its external root is
`/tmp/graphrag-industrial-i5-final`; `capture-input-pins.json` fixes 429 input
files and has SHA-256
`9a199f98e152e1bcbcea6b61801de8b403672c81824acc196ba9809192e6ec4f`.
It uses the same complete command above with that new output root. All 1237
checks passed: 1000 unit, 16 HTTP E2E, 54 security, two regression and 165
Neo4j tests. The Neo4j suite took 1155.414 seconds. Every suite has identical
started/passed inventories and no failures, errors, skips, expected failures or
unexpected successes. The runner reached its final completion marker after
all rebuild, quality, dependency, package, compile, shell and diff gates. The
original tool session was unavailable after the usage interruption; completion
is evidenced by the `set -eu` runner's final marker and its sealed outputs,
not a subsequently retrieved process exit status. Its disposable container
was removed and all 429 captured inputs remained unchanged.

## Reviewed baseline and confirmation

The read-only candidate audit passed. It verifies every original test ID is
retained, and that all existing case digests, contract metrics, diagnostics and
identity domains are identical. The only deterministic change is 285 added
test IDs: 237 unit, 21 security and 27 integration tests relative to baseline
1.9.0. Each added ID resolves to an actual definition in a pinned source file.
The report and candidate both have semantic digest
`857ec1e4c092c0c8a494d57c9837c784f3daa1418dc6b2cb7be01fdcbcdb8e53`.
`baseline-review.json` retains the full inventory and artifact hashes.

The reviewed implementation was copied into the main working directory;
`main-copy-check.json` verifies all 429 implementation inputs matched the
candidate capture. Only the reviewed baseline file and its matching evaluator
version constant then changed to `1.10.0`. Rebuilding the first report against
that baseline passed with the same semantic digest. That report replay is not
counted as a second test execution.

The second complete command is:

```sh
./scripts/run_stage8_validation.sh --repeat 1 \
  --output-dir /tmp/graphrag-industrial-i5-final/confirmation
```

`confirmation-input-pins.json` pins the main working directory before this
execution; `confirmation.log` and `confirmation-exit.json` retain its output
and process exit code. The command exited zero: all 1237 tests passed, with no
failures, errors, skips, expected failures or unexpected successes. The 165
Neo4j tests took 1186.405 seconds. Its disposable database was removed.
All 429 confirmation inputs remained byte-identical. Industrial contract,
corpus, model and frozen-gold validators also passed from the main directory.

Both complete executions retain the same suite inventory and semantic digest;
`compare_evaluation_reports.py` confirms reproducibility. The reviewed baseline
changes no old case result or metric. Its new counts are:

| Suite | Capture | Confirmation |
| --- | ---: | ---: |
| Unit | 1000 | 1000 |
| HTTP E2E | 16 | 16 |
| Security | 54 | 54 |
| Regression | 2 | 2 |
| Disposable Neo4j | 165 | 165 |
| Total | 1237 | 1237 |

Selected completion artifacts, relative to `/tmp/graphrag-industrial-i5-final`:

| Artifact | SHA-256 |
| --- | --- |
| `capture-completion.json` | `67e33d6b7e89c80bd97aa938c8785d2114aa0897c0818bddf2c628b70f0af5dd` |
| `baseline-review.json` | `5c6aacd0923df79e01f5fca4a934ef7ece4bc130115c26a6d021a80b503591cf` |
| `confirmation-input-pins.json` | `996b20b8e77ef2e12d01c785ab11d553e2293b6553da3df3e29ee7f3f0f2813c` |
| `confirmation-completion.json` | `2c2e646f52d1f0466daec14c65229fe826463daab20e5d6eea308cd045ae9023` |
| `confirmation/run-1/report.json` | `2b17f6ff19eec2427d1228b583e4cd002a652edcf0b2cdd9c068e5fba0133536` |

## Scope of the quality claims

The Stage 8 report retains its existing 49 retrieval, 51 answer/conflict and
60 graph cases, plus the locked extraction-quality gate. Industrial unit and
integration tests extend the executed suite inventory. They do not substitute
industrial rankings for the historical gold or redefine existing metrics.

The independently frozen industrial 72-case and three-PDF evaluation remains
documented in [industrial-retrieval.md](industrial-retrieval.md). Revalidating
that captured report checks its integrity and acceptance; it does not make
fresh provider calls or establish new production latency, cost or answer
generation measurements. In particular, its standard fractional Recall@5 must
not be confused with Stage 8's complete-question evidence-group metric.

The actual live upload, AI development review and explicit publication
walkthrough are recorded separately in [industrial-final.md](industrial-final.md).
Test-suite success alone does not claim that those live operations were performed.

## Commit hygiene

The final staged diff passed whitespace checks and the staged-file secret scan
covered 29 files. One unchanged readiness-redaction test dummy URI was initially
flagged; an exact fixture exception was reviewed against the original HEAD
literal and test context. The initial finding and reviewed scan are both kept
under `/tmp/graphrag-industrial-i5`. No real credential, source PDF, database,
cache, virtual environment or unrelated user note is included. The temporary
active-work pointer in `AGENTS.md` is removed; the plan and all validation
history remain.
