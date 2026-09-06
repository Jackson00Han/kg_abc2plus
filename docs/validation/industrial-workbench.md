# I4 industrial workbench validation

Implementation builds on I3 commit `8822f8f`. This milestone adds the reusable
partial graph/source APIs, industrial runtime routing and same-port browser UI.
It does not modify the frozen I3 gold, reranking prompt, model or acceptance
thresholds. I5 records the complete regression and live construction walkthrough.

## Implemented boundary

- `/industrial` uses the existing authenticated service and retained database;
  `/playground` remains available. The industrial flag requires reuse and a
  valid existing industrial load; startup never replaces seed publications.
- Graph browsing binds publication, activation, ontology and corpus versions,
  current authorization and query-bound continuation. Every visible edge has
  exact source evidence; inaccessible records are not revealed as counts.
- Physical composition, classification, diagnostic associations and electrical
  connection are separate views. The ontology view labels allowed type links
  as declarations, not evidence-backed instance assertions. Layout is derived.
- Four industrial personas read their authorized sources. Industrial requests
  use the I3 locked listwise profile; other tenants retain their existing engine.
- Uploads retain source-only or candidate-extraction modes, explicit review
  and explicit publication. Source kind, tenant and immutable applicability are
  server controlled. Existing seed records survive additive publication.
  The walkthrough uses different uploader/reviewer identities; the generic
  review service does not enforce actor inequality for users holding both roles.
- The renderer accepts data and callbacks, holds no credentials, and is separate
  from API/session/UI orchestration. Cytoscape/Dagre are local, pinned licensed
  assets. Source text uses text nodes and exact Unicode codepoint highlighting.

See [graph API](../graph_browsing.md), [runtime/upload contract](../industrial_runtime_and_uploads.md)
and the [Chinese workbench guide](../industrial_workbench.md).

## Failures preserved during validation

The initial new-graph transaction budget of 15 seconds failed on the cold
1-CPU/1536-MiB disposable database. The new graph endpoint now explicitly uses
a 30-second transaction budget, consistent with existing retrieval; provider
limits and the 105-second API request limit are unchanged. The first queries in
two subsequent disposable databases, without query prewarming, completed in
19.125 and 18.518 seconds. These timings exclude container startup and fixture
loading. This is not a
claim of subsecond cold startup. Warm browser readings are recorded separately.

The first combined database run also exposed an incomplete test-loader setup
and cleanup, which were corrected without weakening database constraints. The
second run exposed a corruption-test fixture that accidentally collided with
the existing version/splitter/ordinal uniqueness constraint, and two source-read
errors caused by DateTime parameter roundtripping. Exact epoch-seconds and
nanoseconds now bind the final source-time check; no ACL or source checks were
disabled and no source timeout was enlarged. Failure logs are
retained outside Git and are not counted as passing evidence.

Browser exercise exposed the graph DTO adapter, source reading, resize fitting,
expanded-query pagination and review-operation lifetime boundaries. Repairs
and repeat checks are included in this milestone rather than treating static
rendering as complete functionality.

## Final check evidence

The Chromium Playwright walkthrough used the actual retained service and no
mock source/graph responses. The in-app browser was unavailable. It exercised
27 checks: node/edge exact evidence, bounded expansion, both equipment families,
all six view modes, authority filtering, the 36-source inventory, Canalis PDF
physical pages and adjacent chunks, four personas, construction task listing,
PNG export, and 1024/390-pixel page widths. No page/console errors occurred.
A separate HVX check read original physical page 9 and its official publisher
link. Desktop, tablet, mobile and evidence screenshots were visually inspected.

The pagination exercise reduced only the outgoing page-size limit to one using
Playwright request interception; returned records still came from the real
API. A selected-asset neighborhood continued with the same seeds and pin,
returned HTTP 200 on both pages, and did not repeat the first edge. This test
explicitly records its limit override rather than claiming it was the normal
100-record UI page size.

External read-only evidence under `/tmp/graphrag-industrial-i4/`:

| Artifact | SHA-256 |
| --- | --- |
| `browser-readonly-report-v1.json` | `601c6751960032b1ddcf5c41d28669cf615c4dfd36b94e4db2d67e7af1f4cea9` |
| `browser-layout-report.json` | `d8b681ebc12631701c5f5a83a0c6a3c2b6bdd17548531d50586e7a442327481a` |
| `browser-pagination-report.json` | `c3dad17203b87472471e4e40744c60f9e297d2ac0fe11c7b7b5b6486e2aa38df` |
| `browser-hvx-source-report.json` | `682d323f481c563556dd75e0c362c6d204747f5e04615fdde5e5c195f969a99c` |

Final automated results:

- Full unit suite: 955 passed; full HTTP E2E: 16 passed; full regression: 2 passed.
- Final full security suite: 54 passed, including the last pre-write whitespace
  input rejection. The security inventory validator also passed.
- Final focused Neo4j suite: 14 passed in 400.321 seconds, zero errors or skips;
  SHA-256 `62c36806c674a053226f2e37f983dc7a25936346566858381d1a995722152834`.
  It covers three graph cases, seven industrial upload/source cases and four
  existing graph-projector cases. The previous combined capture also passed
  all eleven unchanged industrial/legacy retrieval cases; its three errors
  were fixed and rechecked in the final focused suite above.
- After the database capture, the final delta only converted invalid blank
  reranker input to the established pre-write HTTP 422 parse-error path.
  All 84 related runtime/upload unit/security checks passed; no source writes,
  graph behavior, provider limits or successful database paths changed.
- The 18 executable Node/static checks passed, including refresh during review,
  identity changes during upload/review/publication, and post-publication
  display failure. Python compilation, Prettier 3.6.2, whitespace and package
  build checks passed. The wheel contains all 18 industrial static assets,
  including vendored graph libraries/licenses and the authored upload example.

Repeat the suites with `scripts/run_test_suite.py --start tests/unit` (and
`tests/security`, `tests/e2e`, `tests/regression`) with separate `--output` paths
and `--require-no-skips`. The focused DB capture used a temporary three-module
selection of the committed industrial runner with unchanged resource and
ownership cleanup guards. To reproduce it as part of the complete database
suite, use:

```sh
./scripts/run_stage8_neo4j_tests.sh \
  /tmp/industrial-recheck/integration.json /tmp/industrial-recheck/observations
```

Changed-file secret checks and the focused commit/push complete I4. The full
Stage 8 baseline refresh, live provider requests, governed browser upload and
restart checks are I5 evidence. No production-candidate qualification is
inferred from `dev-mini`.
