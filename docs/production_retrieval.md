# Production Retrieval Engine

Stage 5 turns the retrieval methods demonstrated in `06_graph_expanded_rag.py`
into the reusable `graphrag_prod.retrieval` package. The numbered script stays
as a learning artifact and now imports the same standard RRF implementation.

## Retrieval pipeline

One Neo4j read transaction performs the complete pipeline against a consistent
corpus snapshot:

1. Read the tenant corpus revision and atomically active embedding generation.
2. Run exact cosine vector recall over authorized active-generation embeddings.
3. Run BM25 recall through the versioned Chunk full-text index.
4. Fuse both rankings with standard Reciprocal Rank Fusion (RRF).
5. Expand bounded seeds through shared accepted Entities and rank candidates
   with the standard Resource Allocation (RA) index.
6. Re-rank the bounded candidate pool by cosine similarity and fuse vector and
   BM25 ranks with RRF again. RA remains candidate generation, not an invented
   third relevance weight.
7. Apply deterministic raw-score/channel gates and exact-content
   deduplication.
8. Attach bounded adjacent chunks, then select whole chunks within both the
   Chunk count and character budgets.
9. Re-authorize and hydrate exact source text and stable provenance for the
   final context.

RRF uses `sum(1 / (k + rank))`, with the documented default `k=60`. RA uses
`sum(1 / degree(entity))` across shared, authorized Entity neighbors. Ties use
stable Chunk IDs. No Neo4j internal `elementId`, custom relevance formula, or
graph fact is returned as answer evidence.

Exact cosine recall deliberately matches the authorized active Chunk set
before scoring. A global approximate vector-index top-N cannot pre-filter
arbitrary access groups and could allow inaccessible vectors to crowd out
authorized results. The active vector generation still fixes the vector space,
dimension, and lifecycle boundary. Representative-scale latency is deferred to
Stage 9 rather than weakening the authorization invariant.

## Security and lifecycle boundary

Every vector, BM25, graph-expansion, adjacency, and hydration query requires:

- the request Principal's exact tenant;
- an intersecting group on both Document and Chunk;
- matching Document/Chunk access-policy identity and version;
- the Document's `ACTIVE_SNAPSHOT` and `ACTIVE_VERSION`;
- Snapshot membership and `OF_VERSION` linkage;
- caller-supplied Document, Version, and publication-time filters; and
- accepted Entity governance state for graph navigation.

BM25 uses a global Neo4j full-text candidate index, but authorization and
version predicates execute in the same database query before any hit is
returned to application code. The BM25 scan and returned rank are independently
bounded. Graph degrees are calculated only from Chunks visible to the same
Principal, so protected connectivity cannot influence the returned RA trace.

Retired versions cannot be re-enabled by a Version filter. Retrieval fails
closed when the tenant has no active embedding generation or the query-vector
dimension differs from that generation.

## Contracts and bounds

`RetrievalRequest` carries the query text, caller-generated query vector, its
embedding-space ID, Principal, `VersionFilter`, and `RetrievalLimits`. The
engine rejects a query space that is not the atomically active generation even
when dimensions happen to match. The embedding provider stays outside the
retrieval engine so later API code can apply provider timeouts, retries, and
accounting without hiding them inside database logic.

`RetrievalLimits` bounds initial recall, BM25 scan, seeds, entities per seed,
graph edges and candidates, the total candidate pool, anchors, adjacency,
returned Chunks, and context characters. It also exposes deterministic cosine,
BM25, and RRF-channel gates. Defaults are safe reference values, not deployment
tuning claims.

`minimum_vector_score` uses Neo4j's cosine-similarity score domain, not the raw
mathematical cosine domain. Neo4j returns values in `[0, 1]` and maps orthogonal
vectors to `0.5`; see the
[Neo4j 5 vector-index definition](https://neo4j.com/docs/cypher-manual/5/indexes/semantic-indexes/vector-indexes/).
Thresholds must therefore be calibrated and recorded in that same domain.
Negative thresholds are rejected because they cannot gate any Neo4j cosine
result.

Exact-content deduplication is scoped to an immutable Version. Identical text
inside one Version can be collapsed, but matching checksums from different
Versions remain separate so temporal or conflicting provenance cannot be
erased.

Context budgeting never truncates Chunk text. A Chunk that cannot fit is
skipped and recorded in the trace, preserving its checksum and exact character
location if selected elsewhere.

## Result and trace

Every `RetrievedChunk` contains source text plus a stable `Citation` with:

- Document ID, canonical URI, and source name;
- immutable Version ID, checksum, and version number; and
- Chunk ID, checksum, ordinal, character range, page, and section.

`RetrievalTrace` records the corpus revision, embedding generation/space, all
bounded rankings, per-channel ranks, RA reasons, gates, deduplication, budget
decisions, selected Chunk IDs, configuration, and Version filter. Its ID is a
deterministic hash of the complete request and active retrieval state.

The trace contains only authorized stable IDs and metadata. It does not include
protected candidates removed inside Neo4j.

## Evaluation

`evaluation/retrieval-gold-v1.json` is a versioned 49-case regression fixture:
five success and two boundary cases for each Stage 1 question class. It stores
exact Chunk-level relevance grades, deterministic baseline rankings, and any
unauthorized exposures.

Run the evaluator:

```bash
uv run --locked python scripts/evaluate_retrieval.py
```

The evaluator calculates the Stage 1 definitions for Recall@5, MRR, nDCG@5,
and unauthorized exposure count. Stage 8 will connect this metric code to the
unified automated evaluation workflow; Stage 9 will run representative scale
and concurrency measurements.
