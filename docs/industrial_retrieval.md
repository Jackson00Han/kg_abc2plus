# Industrial retrieval scope (I3)

The existing `POST /v1/retrieval` and `POST /v1/answers` routes accept an
optional `industrial_scope`. The query backend requires an injected
`Neo4jIndustrialScopeResolver`; requesting this feature without that resolver
returns a dependency error before calling the embedding provider. Omitting the
field preserves the existing route behavior.

```json
{
  "query_text": "HVX-A01 本地和远方模式核对有哪些证据？",
  "industrial_scope": {
    "family": "evopact-hvx-up24",
    "asset_keys": ["asset-hvx-a01"],
    "include_references": true,
    "source_kinds": []
  }
}
```

`family` accepts `canalis-kt` or `evopact-hvx-up24`; omission selects those two
core families. `asset_keys` contains at most eight canonical lowercase corpus
identities. It is a caller-supplied filter, not automatic entity resolution from
the question. Corpus keys such as `asset-hvx-a01` differ from display equipment
codes such as `HVX-A01`; changing case alone does not resolve the identity.
`source_kinds` optionally narrows to `CURATED_REFERENCE`,
`OFFICIAL_PUBLICATION`, and/or `SYNTHETIC_FIELD_RECORD`. Empty source kinds means
all three. `include_references=false` excludes curated and official reference
documents. Metadata-only and applicability-excluded sources are not silently
included as reference evidence.

The resolver uses immutable industrial facets on the current `DocumentVersion`.
Every source query requires the authenticated tenant and groups, an active
document version, a published active snapshot of that same version, a chunk in
that snapshot, and consistent document/chunk ACL metadata. It intersects the
ordinary document/version filters and publication cutoff at that boundary.
The result is at most 100 current version IDs. It does not return stale
historical versions or truncate a larger scope silently; exceeding the bound
requires a narrower request.

When assets are requested, every identity must first occur in an authorized
current field document satisfying the original version filter and optional
family. Only then are references from those resolved families included.
Unknown, inaccessible, incompatible, and cutoff-excluded identities all produce
the same empty result. For example, a request restricted to manual version IDs
cannot simultaneously establish a field asset's identity. Narrowing source
kinds to references still permits the separate authorized field identity check.

`VersionFilter.match_none` is a strict boolean defaulting to false. It
distinguishes an empty resolved scope from the longstanding meaning of omitted
or empty ID lists (no restriction). Empty scopes perform no vector, BM25, graph
expansion, adjacency, or hydration queries. They still verify the tenant's
active embedding generation, so an unavailable runtime remains a dependency
failure rather than fabricated retrieval success. The API and evidence
projector also enforce the empty outcome. Answers use the existing
`insufficient_context` behavior without calling the answer model.

Resolution checks the corpus revision before and after its read transaction,
with at most one retry. Resolved version IDs prevent a later active-version
change from widening the request. Retrieval independently captures both corpus
and embedding state plus the active knowledge publication ID and monotonic
activation generation. A publication activation, including none-to-active or a
rollback to the same publication, invalidates the captured result and uses the
existing bounded retry. The publication ID in the trace is a tenant operational
revision; it grants no access to publication members or protected source IDs.
This pin covers the engine's reads. The optional `include_graph` projector is
currently a subsequent independent read and may observe a later publication;
the combined response is not yet promised to use one publication throughout.
The graph workbench stage must carry that pin into projection and pagination.

The trace reports only the requested filters and matched authorized document
counts, reference count, match-none state, and the scope policy version. It
does not expose inaccessible asset identities or hidden-source counts.

## Graph and ranking behavior

Governed publication materializes approved, exact-evidence entity mentions
into the existing published source snapshots. Resource Allocation can therefore
expand from an industrial source chunk to another authorized chunk sharing an
entity. This is shared-entity navigation; it does not perform multi-hop searches
over diagnostic predicates such as `MAY_INDICATE` or `CHECKED_BY`. The evidence
projector remains a bounded post-retrieval graph projection. Graph nodes are
navigation data; answer evidence continues to be source chunks.

This scope change retains the existing vector/BM25 recall, standard RRF,
Resource Allocation, relevance gates, deduplication, and semantic-section
adjacency. It changes neither full-text analyzers nor scoring. In particular,
BM25's existing bounded scan applies industrial version filters after its
tenant/access partition; restrictive scopes can still lose candidates at that
scan boundary. Independent gold evaluation must measure this limitation and
context allocation before any ranking or analyzer upgrade is justified.

## Checks

Routine checks need no providers or database:

```sh
.venv/bin/python -m unittest tests.unit.test_industrial_retrieval tests.security.test_industrial_retrieval_api tests.security.test_retrieval_no_existence_signal
```

The disposable Neo4j runner additionally includes
`tests.integration.test_industrial_retrieval_neo4j`. Its one test loads only two
authored sources and proves governed publication-to-RA materialization, active
publication tracing, group isolation, asset/reference scoping, unknown and
foreign identities, version/cutoff restrictions, and stale ACL/snapshot
rejection. Its small fixture is correctness evidence, not retrieval-quality or
corpus-scale evidence. Quality results belong in the independent I3 evaluation
report.

## Optional two-phase reranking

`Neo4jRetrievalEngine(..., reranker=provider, rerank_candidate_limit=50)` enables
an additional ranking stage. Omitting the provider retains the previous
retrieval behavior. The first read reuses the existing recall, RRF, relevance
gates, exact source hydration and deduplication, and prepares at most 50 intact
authorized chunks. The provider receives their raw text/checksums and exact
source title, section, document ID and version ID as separate metadata.

The provider runs once outside both Neo4j read sessions. A second read checks
the captured corpus revision, embedding generation and publication activation,
then reauthorizes every chunk ID exposed by the trace, including candidates
that were not sent to the model. It verifies source text, checksums and rendered
source metadata have not changed. Context selection and semantic adjacency use
the original request's chunk and character limits after the new ranking.
Publication changes, an ACL revocation, invalid provider output or a failed
final read fail the request; they do not trigger another paid call or a silent
fallback.

Provider rank order is canonical. Numerical reranker scores are retained as
observations separately from RRF; a listwise provider may return `score=null`.
These values are not fact confidence and are not compared between questions.
Each candidate must occur exactly once in the final ranking. A separately
declared listwise normalization may stably remove duplicate indices only when
the original list contains all candidates and no unknown index. Its policy,
removed count, original zero-based permutation and raw output checksum remain
in the trace. It cannot invent missing candidates or alter first-occurrence
order.

`reranking` trace metadata records input candidate IDs, final ranked IDs, model,
rendering version, raw source checksums, rendered-input checksums and canonical
provider input/output digests. Raw HTTP response bodies belong only in the
private evaluation cache. Observed prompt/completion/total token counts remain
separate: a provider reporting only total tokens does not acquire an invented
prompt/completion breakdown. Legacy input/output counters support API usage
aggregation, with the observed fields providing the precise audit semantics.
Provider cost is unmeasured unless the deployment supplies pricing accounting.

For an explicitly chosen scope-aware profile, trusted assembly may set
`GraphRAGQueryOperations(include_industrial_rerank_context=True)`. It renders
only the validated caller-supplied asset keys with
`build_rerank_scope_context()` and passes the bounded internal
`RetrievalRequest.rerank_context`. The provider receives the original question,
a newline and this scope context. Vector and BM25 recall still use the original
question. `rerank_context` is not an accepted HTTP field. This setting defaults
to false, and the legacy omission behavior remains unchanged.

The API already restricts automatic retries to its provider-free read
whitelist; retrieval and answering were not retried automatically. The new
`RerankAttemptedFailure` boundary additionally gives callers explicit
`retryable=false` 503/504 errors once a reranking call might have been charged.
This is an added paid-stage error boundary, not a change to the existing
provider-free retry rules. Before-provider candidate preparation keeps the
existing bounded consistency retry.

Additional routine checks:

```sh
.venv/bin/python -m unittest tests.unit.test_retrieval_reranking tests.security.test_reranking_api tests.unit.test_api_runtime
```

The two-source Neo4j test also checks an unscored model ranking, publication
activation ABA during the external call and post-call ACL revocation. Its
deterministic provider performs no network calls and does not claim model
quality. The standard two-stage retrieval/reranking approach is described in
the [Sentence Transformers documentation](https://www.sbert.net/examples/sentence_transformer/applications/retrieve_rerank/README.html).
