# I3: industrial retrieval and live corpus validation

This record accompanies the third industrial milestone. It measures the
industrial development workload; it does not extend the historical Stage 9
production-candidate qualification or claim real field-diagnosis accuracy.

## Live source baseline

The unchanged I2 implementation at
`feade8048d2dd34344fefd015b17b8ef43464f68` loaded the medium corpus and independently
verified official excerpts into the retained local development database. The
configured DashScope `text-embedding-v4` provider generated the 1024-dimensional
source vectors; no fixture vectors were substituted.

| Item | Recorded value |
| --- | --- |
| Tenant | `industrial-schneider-demo` |
| Versioned sources | 36: 34 authored documents and 2 selected official excerpts |
| Source Chunks | 370: 330 authored and 40 official excerpt Chunks |
| Published governed records | 193 |
| Corpus checksum | `f79af571b20364e08d2a966da129714b417b6445cd46f5744339697050d06703` |
| Combined load manifest | `ff3e15cdaa3c1b888ede209144dd1a3bbef2d33c2eb1feb4e9043373ecf660fb` |
| Knowledge publication | `702c16fd-dd9d-52fb-81a7-ca832006952f` |
| Embedding generation | `8719e667-3aa5-560d-ac7a-c93e3443cdd8` |

The external load record is `/tmp/graphrag-industrial-live-load.json`. Original
PDFs, normalized source text, raw captures and query vectors remain outside Git.
The loader did not capture provider billing usage; source embedding calls,
tokens and cost are therefore unknown, rather than inferred from Chunk count.

Existing data were checked before and after loading. Excluding industrial
tenant nodes and its newly created ingestion tasks and exclusively referenced
pipeline profiles/governance policy leaves the same 1,167 original nodes and
the same 12 original documents (8 alpha and 4 beta). Their aggregate sorted
label/property checksum is unchanged:
`4984fc3233b39005cd21213ba6b476adf4df9a3adf34e7a09ef5c8c40d85d4df`.
Serialization uses sorted labels, sorted JSON keys, `ensure_ascii=False`, the
default JSON separators and `default=str`; sorted node JSON strings are joined
with a newline, without a trailing newline. This is a node/property preservation
check, not a separate exhaustive relationship audit. The paired local records
are `/tmp/graphrag-industrial-i2/existing-service-before.json` and
`/tmp/graphrag-industrial-i2/existing-service-after.json`.

## Frozen evaluation inputs

The independent source review froze 72 questions before any predictions, with
36 development and 36 holdout cases. Gold version `industrial-gold-v1.0.1` has
checksum `c44392ee640c7963323519ccebe7b7d1111cdffe470a5fe48be80bc3c238fd34`.
The earlier 1.0.0 identity was withdrawn after the final independent identity
review arrived; no predictions were run against it. Three separately reported
official-PDF context questions use manifest checksum
`a8efea61e6907b74ccbcfcf9253c402e87ed73458d3089bc5bde81fef0d34102`.

The comparison uses the immutable I2 checkout for the legacy engine and pins
the actual source bytes of the new engine. All variants receive the same query
vectors. Development results select configuration; captured holdout results
remain unread until that selection is fixed. See
[gold annotations](../industrial_retrieval_gold.md),
[scope behavior](../industrial_retrieval.md), and
[evaluation commands and measurement boundaries](../industrial_retrieval_evaluation.md).

## Results and exit checks

The initial immutable comparison has report checksum
`62eb7a0f67e0f34a10a1e4e526b02039ee4069b13fb511f58324e3f84397181f`.
Only its development results were read to select the next experiment:

| Development configuration | Recall@5 | MRR | nDCG@5 | Complete evidence set |
| --- | --- | --- | --- | --- |
| I2 engine | 0.4540 | 0.8229 | 0.5359 | 0.7500 |
| Industrial scope, original context allocation | 0.4915 | 0.8646 | 0.5768 | 0.7500 |
| Industrial scope, five ranked anchors | 0.5819 | 0.8646 | 0.6212 | 0.8438 |

All 36 development cases, including four denied requests, remain in each
configuration's report. The 32 cases with an authorized positive evidence target
contribute to ranking metrics. Both scoped variants have zero current-ACL,
scope, citation, sentinel and runtime errors. The I2 baseline has 796
out-of-request-scope Chunk occurrences across exposed retrieval traces; these
are applicability failures, not cross-tenant or access-group leaks.

The source-reviewed development candidate analysis found 115 of 118 positive
anchors in the existing candidate pools. Their mean candidate recall is 0.9774;
an ideal top-five ordering could reach mean Recall@5 0.9425 and nDCG@5 0.9974.
Every positive development case already has a complete evidence alternative in
its candidate pool. These are measured candidate ceilings, not achieved final
ranking scores. They justify testing standard reranking before increasing
recall budgets or changing analyzers.

The initial scoped configuration does not pass the existing Recall@5 0.85 and
nDCG@5 0.80 targets. Its independent development acceptance assessment is
`f9e5ccc14a8da674a26109127418991311990767335fa1005373f89723f9b846`, status
`FAILED`. Successful capture/validation is not relabeled as quality acceptance.

The initial implementation passed 870 unit tests, 16 HTTP E2E tests, 40 security
tests and two regression tests. The separately bounded Neo4j run passed all 15
industrial/legacy retrieval and graph-projection integration tests, without
skips, in 399.521 seconds. The container used one CPU and 1536 MiB with a 512 MiB
maximum heap and 128 MiB page cache, and was removed after the run. Evidence is
under `/tmp/graphrag-industrial-i3/` and
`/tmp/graphrag-industrial-i3-retrieval-check1/`. Later reranking changes require
their own relevant rerun before this milestone can complete.

The evaluator's independent review also found a blocked-stdin deadline gap.
It was fixed with nonblocking partial writes under the same request deadline,
then tested with a child process that never reads stdin. New captures additionally
pin publication activation generation; the earlier report is retained without
retroactively claiming that stronger pin. Selected-variant acceptance can report
development alone without revealing holdout predictions.

## Development-only reranking selection

Before opening holdout predictions, bounded local probes reused the actual
scoped candidate order and rehydrated it against the current database's tenant,
ACL, version and publication state. They sent no gold labels or expected IDs to
models. All candidates retained their original text; contextual variants added
the current authorized document title and section solely as ranking input.

| Development probe | Recall@5 | MRR | nDCG@5 | Complete evidence set |
| --- | --- | --- | --- | --- |
| `qwen3-rerank`, default QA | 0.6431 | 0.8854 | 0.7005 | 0.8125 |
| Same model, contextual QA | 0.6547 | 0.9245 | 0.6885 | 0.8438 |
| `qwen3.8-max`, listwise contextual instruction | 0.8106 | 0.9453 | 0.8694 | 1.0000 |
| Same model, listwise evidence instruction (selected) | 0.8254 | 0.9453 | 0.8824 | 0.9688 |

Listwise permutation ranking follows the established method family described
in the [RankGPT paper and implementation](https://github.com/sunnweiwei/RankGPT).
It returns an order, with no invented relevance probability or weighted blend
with RRF. The selected instruction emphasizes direct requested evidence,
qualifying conditions, faithful summaries and evidence of missing information.
It does not contain corpus-specific answers, case IDs or expected source IDs.

One contextual-probe response contained all 31 valid indices and then repeated
one index. The original strict-parser run failed and was retained. A separately
identified replay applied stable removal of repeated indices, recording one
removal; it did not insert missing indices or call the provider again. The
formal listwise policy requires every valid input index to be present, rejects
invalid or missing indices, bounds original output to twice the candidate count,
and records the original permutation, response checksum and normalization count.
The selected evidence-instruction probe completed all 36 development cases.

The selected profile is `listwise-evidence-v1`, using the already configured
`qwen3.8-max` model on the Beijing DashScope endpoint, temperature zero, disabled
thinking, a 1,024-token output cap and a 30-second total provider deadline. This
is an explicit model alias, not a claim of immutable weight-version pinning.
The selection was recorded before holdout inspection at
`/tmp/graphrag-industrial-i3/selected-profile-before-holdout.json`, checksum
`e7f53850503dc2723d72f4c4c6b11adfd4b42c4feebf1c807d2078d9025cc5c4`.
Its system-prompt checksum is
`8c7f6167680ead2d380a4f624631f7b1e958ef9ec180c0a558616cb4aaa51f74`.

Selection favors the two primary ranking metrics; it also records the one-case
complete-evidence decrease from the preceding listwise probe. Development
Recall@5 remains below 0.85 and is not presented as passing that threshold.
Formal implementation checks and full 72-case/holdout acceptance are still
required. Independent parser validation confirmed all 35 observed, nonempty
selected-probe request payloads match the formal provider's payloads and their
responses are accepted. This check made no model calls and did not import
placeholder timings into the runtime cache.

## Formal implementation validation

The formal development capture used fresh provider calls, rather than importing
probe results. Its report checksum is
`b91d5123961cbe7a631f5438c9d192041e2ecc38f7dd2c173293cbd41d9034c8`.
Across all 36 development cases, Recall@5 is 0.8228423, MRR is 0.9453125,
nDCG@5 is 0.8825256 and complete evidence-set coverage is 0.96875. All current
ACL, requested-scope, citation, sentinel and runtime error counts are zero.
Recall remains below the development threshold. The small change from the
prototype is retained as observed provider variability; the selected prompt
and model alias were not changed. The 35 nonempty requests used 156,663 prompt
tokens and 3,071 completion tokens, as reported by the provider. All 36 query
embeddings came from the validated existing vector cache.

The final implementation passes 906 unit, 16 HTTP E2E, 44 security and two
regression tests, without skips. A second bounded real-Neo4j run passes all
15 industrial/legacy retrieval and subgraph tests in 351.523 seconds, including
successful listwise ranking with absent numerical scores, publication
activation ABA changes and ACL revocation during provider execution. Its
separate container was removed; the retained playground database was not
reset. Evidence is under `/tmp/graphrag-industrial-i3/` and
`/tmp/graphrag-industrial-i3-retrieval-check2/`.

## Final acceptance and measured limitations

The locked profile was then run on all 72 primary cases and the three separate
PDF questions. Only after completion were the holdout predictions opened. The
full report checksum is
`275dec92d8a258a47d4466bfc9426de193d05e2e982de6a44ee3ace778814eaf`;
the independent acceptance checksum is
`c3b68e3db3016fc26062bfd24fdf492457fefdbbabe0f9e0e2e1b53fa7b7f1c7`.
Actual implementation source checksum:
`5fecb3af02ecac07638758bf56dbd0664debff9175bdae9a0662a5417aa52c6f`.

| Complete primary gold | Recall@5 | MRR | nDCG@5 | Complete evidence set |
| --- | --- | --- | --- | --- |
| I2 baseline | 0.4731 | 0.7656 | 0.5127 | 0.7188 |
| Scope only | 0.5392 | 0.8099 | 0.5643 | 0.7500 |
| Scope and five ranked anchors | 0.6131 | 0.8099 | 0.5963 | 0.8125 |
| Selected listwise profile | **0.8635** | **0.9531** | **0.8629** | **0.9375** |

The complete 72-case primary population passes the unchanged Recall@5 0.85,
MRR 0.80 and nDCG@5 0.80 thresholds. The 36-case holdout independently passes,
with Recall@5 0.9042, MRR 0.9609, nDCG@5 0.8433 and complete evidence coverage
0.90625. The development assessment remains `FAILED` on recall. All eight
denied cases and their controls remain present. Selected-profile scope, ACL,
sentinel, citation and runtime error counts are zero. The full I2 baseline
records 1,558 out-of-request-scope trace occurrences, with zero ACL leaks.

The official-PDF suite ran against independently rehashed and reparsed originals.
Its Recall@5 is 0.8056, MRR 1.0 and nDCG@5 0.8657, with zero citation or access
errors. Only the HVX MX1 table question has a complete required evidence set.
The Canalis cross-page conditions question misses one of four relevant Chunks;
the HVX unexpected-trip question misses one of three. These two incomplete
contexts are an explicit retrieval limitation. The three-case suite is not
merged into the primary acceptance population and is not claimed to pass a
complete-context gate. Opening the exact source context remains necessary for
these questions; partial retrieval does not establish a complete procedure.

The full run reused 35 validated development rankings and all 75 query vectors.
It made 37 new reranking calls, reporting 165,706 prompt and 3,169 completion
tokens. These counts exclude the earlier development calls and probes. The
fresh development run's median per-case worker duration was 4,878.9 ms, p95
6,126.9 ms by linear interpolation, maximum 6,383.7 ms. These times include
database work and ranking, exclude query-vector creation and browser overhead,
and are not a production latency qualification. Mixed cache/live full-run
timings must not be presented as fresh-provider latency. No billed currency
amount is inferred. A local timeout can stop the worker without proving remote
cancellation or absence of charges.

A redistributable summary, excluding original PDF text, raw responses and
credentials, is committed at
[`baseline.v1.json`](../../datasets/industrial-v1/evaluation/baseline.v1.json).
Reproduce integrity and acceptance from the external captures with:

```sh
uv run --locked python scripts/evaluate_industrial_retrieval.py \
  --validate-report /tmp/graphrag-industrial-i3/rerank-all-v2.json \
  --expected-pins /tmp/graphrag-industrial-i3/rerank-all-v2.pins.json \
  --selected-variant scoped_reranked --split all
```

Offline report recomputation, compilation, package build, deterministic
gold/corpus/model checks, whitespace checks and the changed-file secret scan
pass. The focused I3 commit/push records completion. I4 implementation starts
after that push; graph UI, live HTTP routing and final browser validation are
not included in these I3 results.
