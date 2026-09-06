# Authorized graph browsing (I4)

The graph workbench uses reusable reader APIs rather than the operational
publication inventory. `knowledge:graph:read` permits an authorized partial
view; it does not grant `knowledge:quality`, publication management, or access
to the rest of a publication. The existing inventory retains its complete ACL
requirement. Graph routes perform no embedding or language-model calls.

## API assembly

```python
from graphrag_prod.api.graph import Neo4jGraphOperations
from graphrag_prod.graph.browsing import Neo4jPublishedGraphBrowser

graph = Neo4jGraphOperations(
    browser=Neo4jPublishedGraphBrowser(driver, database),
    industrial_scope_resolver=industrial_scope_resolver,
    source_catalog=industrial_source_catalog,
)
backend = GraphRAGApplicationBackend(
    documents=documents, queries=queries, readiness=readiness,
    knowledge=knowledge, graph=graph,
)
```

The optional `graph` dependency is separate from the existing knowledge writer
protocol. Missing dependencies fail closed. The browser can also be called
without HTTP. Industrial source scope resolution belongs to the facade and
uses the authenticated Principal; the graph service accepts a bounded ordinary
`VersionFilter`, preserving use by other domains.

## Browsing, filters and expansion

`POST /v1/knowledge/graph:query` accepts:

```json
{
  "industrial_scope": {
    "family": "canalis-kt",
    "asset_keys": ["asset-bkt-a01"],
    "include_family_references": true,
    "source_kinds": []
  },
  "trust_policy": "PUBLISHED_SECONDARY_INCLUSIVE",
  "entity_types": [],
  "predicates": ["PART_OF", "INSTALLED_AT", "INSTANCE_OF"],
  "name_query": null,
  "seed_entity_ids": [],
  "direction": "both",
  "hops": 1,
  "page_size": 100,
  "view_token": null,
  "cursor": null
}
```

An empty seed list browses matching authorized records. Up to eight explicit
entity IDs start bounded expansion; unknown or inaccessible seeds yield the
same empty selection and never fall back to the whole graph. Expansion accepts
one or two hops and `both`, `outgoing`, or `incoming`. `entity_types` and
`name_query` select anchors; returned assertions retain both endpoints even
when the other endpoint has another type. No custom Cypher is accepted.

Predicate filtering returns matching assertion endpoints plus explicit seeds;
unrelated isolated mentions do not crowd a filtered view. Without predicate
filters, visible mentions can appear independently. Classification (`SUBTYPE_OF`)
and physical composition (`PART_OF`) are separate T-Box hierarchy declarations.
Electrical connection and diagnostic relationships retain their own meanings.
Any roots or depths derived by the UI apply only to the current authorized
view. They do not establish global roots, inherited properties or causality.

The response has `view_token`, `pin`, `schema`, `nodes`, `edges`, `literals`,
`page`, and `visibility: AUTHORIZED_SOURCE_VIEW`. Nodes expose `entity_id`,
`entity_type`, `label`, `canonical_key`, `authority_levels`, and currently
visible `mention_revision_ids`. An edge keeps its assertion `revision_id`,
`record_id`, `source`, `target`, `predicate`, `authority_level`, `origin`,
`confidence`, and optional `source_kind`. Parallel source assertions are not
merged. Mixed node authority remains a set, not an invented trust score.
Literal facts keep their original value and optional standard typed semantics,
including units and temporal qualifiers. The industrial I2 corpus still has
source-only measurements; this API does not manufacture numeric assertions.

`schema` is the T-Box bound to that publication, including its type, relation
and hierarchy definitions. A still-bound retired T-Box is valid. The service
never substitutes an unrelated latest T-Box. The UI must identify these as
schema declarations, separate from evidenced instances.

## View tokens and bounded pages

Each response contains a pin with `publication_id`, `publication_generation`,
`activation_generation`, `ontology_version_id`, `tbox_checksum`, and
`corpus_revision`. Publication generation and activation generation differ:
reactivating the same publication changes the latter and invalidates the view.

A signed view token binds the Principal, groups and action capabilities, the
resolved source/version filter, trust policy, pin, authorized-view digest, and
expiration. A cursor additionally binds the exact selection and last stable
ordering key. Keep the original filters when continuing a page. Changing the
selection with a view token is allowed when no cursor is supplied; changing
source scope or trust policy starts a new view.

Tokens are valid for ten minutes by default. The default per-process random
signing key makes a process restart invalidate existing views. Deployments
needing continuity across workers can inject a deployment-managed key; no
signing key or source text is returned in a token. Tokens never confer data
permissions. Each read rechecks current source and record ACLs.

A view change returns HTTP 409 with `code: graph_view_changed`. The client must
discard the accumulated graph and request a fresh view. Hidden/nonexistent
records return indistinguishable empty results. The response does not expose
whole publication manifests, hidden counts or global source statistics.

The publication bound is 500 total mention and assertion records, not 500
edges. The browser reads at most 501 authorized rows per record kind as an
overflow sentinel and accepts at most 500 combined mention/assertion records,
with bounded source metadata/text. It reuses the existing strict evidence
projector to validate individual records, and rereads its authorized rows and
state before returning from a timed transaction. This catches corpus changes
and ACL changes that did not advance the usual corpus revision. An over-budget
view fails explicitly and requires a narrower source scope.

Pages contain at most 150 nodes and 200 edges, with both edge endpoints present.
`page_size` counts selected assertion/node entries and is at most 200. Endpoint
nodes can repeat across pages; assertion revisions have stable unique positions.
`page.has_more` and `next_cursor` concern authorized matches only. They do not
indicate whether inaccessible neighbors exist.

## Exact graph evidence and source-only reading

`POST /v1/knowledge/graph:evidence` takes a `view_token` and one to ten
`revision_ids`. It returns `{view_token, pin, items}`. Each item has record
identity, origin/authority/status/confidence, `reviewed_by`, `reviewed_at`,
`review_notes`, `source_kind`, `applicability`, and the existing `evidence`
structure. `evidence.citation` contains the full bounded `chunk_text`, chunk and
version checksums, document title, source URI, version, section/page and exact
chunk range. `evidence.char_start/char_end` delimit the exact quote using
absolute document offsets. Highlight within the chunk by subtracting
`citation.char_start`. The combined response is bounded to 500,000 evidence
characters. An unknown or unauthorized revision is omitted identically.

Governance review is not manufacturer or SME approval. Where no independent
SME review is recorded, `applicability.sme_review_state` is `NOT_RECORDED`.
The explicit `project_curated_not_company_approved` and `is_synthetic` source
flags remain separate. No review status or diagnostic certainty is inferred
from a graph layout or ranking score.

Sources without graph assertions remain readable through two reader endpoints:

* `POST /v1/industrial/sources:query`: `{family, asset_keys, limit}` returns
  `{sources, has_more}`. Each source has document/version IDs, title, kind,
  family, asset keys, publication time, authorized chunk count, first chunk ID,
  and optional canonical URI.
* `POST /v1/industrial/sources:chunk`: `{chunk_id}` returns `{chunk: object|null}`.
  The object includes source metadata, exact text/checksum/range, section/page,
  bounded physical provenance, and optional authorized same-version
  `previous_chunk_id`/`next_chunk_id`.

These endpoints require `retrieval:read`. They do not require a graph publication
or lifecycle/quality management capability. Source-only uploads are valid
source records, not graph errors. Manual adjacent-chunk browsing is an audit
feature and is not included in the frozen I3 retrieval metrics.

## Retrieval responses with graph context

The retrieval engine additionally captures its bound T-Box and publication
generation. `include_graph` forwards the resulting full pin into the projector.
The projector performs a timed managed read, rechecks authorization and the pin,
and the API validates returned publication/T-Box identities. A switch between
retrieval and graph projection fails the combined response rather than mixing
two publications. All selected source chunks are reauthorized before and after
projection, including sources without graph records. Revoking such a source
without advancing the corpus revision still rejects the combined response.
Failure after optional paid reranking never transparently
repeats that paid call.

The shared assertion query now selects the seed entity/chunk as a pair from one
ordered row. Independently taking the minimum entity ID and minimum chunk ID
could invent a pair that never occurred in a multi-document graph. A four-source
integration fixture explicitly covers that failure mode.

## Checks

```sh
.venv/bin/python -m unittest tests.unit.test_graph_browsing tests.security.test_graph_browsing_api tests.unit.test_api_backend tests.unit.test_retrieval_subgraph
```

`tests.integration.test_graph_browsing_neo4j` uses a disposable database, tiny
authored industrial sources and deterministic fixture embeddings. It checks
partial reader access, pagination, precise provenance, source/ACL changes,
publication ABA, combined retrieval projection pins and cross-document seed
pairing. Full industrial corpus loading, provider calls and production scale
qualification are not part of these routine checks.

The browser and pinned projection use a 30-second transaction budget. The
initial 15-second implementation timed out during first assertion planning on
a fresh one-CPU database; that failed run is retained as development evidence.
The integration fixture does not prewarm browser queries and records its first
read duration. These correctness checks do not establish production latency
targets.

The second isolated run's first browser query completed in 19.125 seconds with
no query warmup under one CPU, 1536 MiB container memory, 512 MiB heap and 128 MiB
page cache. This measurement excludes container startup and fixture ingestion.
The final run repeated that first read in 18.518 seconds and passed all 14
graph/source/projector tests in 400.321 seconds with no skips. The preceding
broader run also passed all 11 unchanged retrieval tests; its three failures
(one fixture uniqueness collision and two source timestamp comparisons) were
fixed and covered by the final run. The 126 focused unit/API/security checks
also passed. These are development correctness results, not production load
qualification.

To reproduce the focused database check from the repository root, this command
copies only the test-discovery selection of the tracked bounded runner. It
preserves its loopback-only container, resource limits, provider environment
removal, empty-database guards and owned-container cleanup:

```sh
.venv/bin/python - <<'PY'
from pathlib import Path
import shlex
import subprocess
import tempfile

root = Path.cwd()
output = Path(tempfile.mkdtemp(prefix="industrial-graph-check-"))
with tempfile.TemporaryDirectory(prefix="industrial-graph-modules-") as temporary:
    directory = Path(temporary)
    for name in (
        "test_graph_browsing_neo4j.py",
        "test_industrial_upload_neo4j.py",
        "test_retrieval_subgraph_neo4j.py",
    ):
        (directory / name).symlink_to(root / "tests/integration" / name)
    script = (root / "scripts/run_industrial_neo4j_tests.sh").read_text()
    script = script.replace("--start tests/integration", "--start " + shlex.quote(str(directory)))
    script = script.replace("'test_industrial_*neo4j.py'", "'test_*neo4j.py'")
    runner = directory / "run.sh"
    runner.write_text(script)
    subprocess.run(["sh", str(runner), str(output / "suite.json"), str(output / "observations")], check=True)
print(output)
PY
```
