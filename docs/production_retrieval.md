# Production Retrieval Engine

Stage 5 turns the retrieval methods demonstrated in `06_graph_expanded_rag.py`
into the reusable `graphrag_prod.retrieval` package. The numbered script stays
as a learning artifact and now imports the same standard RRF implementation.

## Retrieval pipeline

One Neo4j read transaction performs the complete pipeline against a consistent
corpus snapshot:

1. Read the tenant corpus revision and atomically active embedding generation.
2. Match only authorized Chunks in the atomically active Version and embedding
   generation, then compute exact cosine recall over that set.
3. Run BM25 recall through the v2 Chunk full-text index, with tenant,
   active-publication, and access-group partition terms inside the Lucene query.
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

At the HTTP application boundary, an optional governed subgraph projector can
use the final trace's selected Chunk IDs as seeds. It returns only active,
published, evidence-bound Entity mentions and one-hop assertions under either
the inclusive published-secondary policy or the authoritative-only policy.
This projection is a separate response field with its own limits, exact Chunk
citations, and latency stage. It does not alter ranking and is never passed to
answer generation as factual evidence.

The projector receives the engine's effective `VersionFilter`, rather than a
new client-derived filter. Document IDs, version IDs, and the inclusive
`published_at_or_before` cutoff are enforced inside both seed and one-hop
evidence queries and checked again during projection. An expanded assertion
cannot reintroduce evidence excluded from initial retrieval. Each publication
must also have one exact, same-tenant `USES_TBOX_VERSION` binding matching its
manifest revisions; missing or additional T-Box edges fail closed. The bound
T-Box may be `PUBLISHED` or `RETIRED`, preserving immutable historical
publication replay without reinterpreting it under the current ontology.

Projected literal assertions and matching one-hop paths preserve the complete
server-normalized typed literal semantics (raw/canonical value and unit plus
temporal validity/observation qualifiers). Every non-null raw token remains
bound to the assertion's exact evidence span. A legacy published assertion can
have `literal_semantics: null`; a partially stored or internally inconsistent
typed group fails projection rather than silently degrading to legacy data.

Vector recall deliberately does not take a global approximate-index top-N
window. Its Cypher first matches the request tenant, active Snapshot and
Version, compatible active embedding generation, Document and Chunk ACLs,
access-policy identity, and optional Version filters. It then evaluates
`vector.similarity.cosine` for those rows, applies the score gate, orders by
score and stable Chunk ID, and finally applies `vector_recall_k`. Consequently,
same-tenant ACL-hidden, historical, and cross-tenant vectors cannot crowd an
authorized result out of an earlier candidate window or alter the returned
trace.

The active generation is revalidated inside each exact-vector query by its
generation ID, corpus revision, embedding-space ID, and dimensions in a
single-row subquery. Candidate re-ranking starts from the bounded candidate
IDs through the unique Chunk ID index, then repeats the complete active,
tenant, ACL, policy, Version, generation, and vector-shape checks before
cosine scoring. This changes the query plan, not ranking or authorization
semantics.

A read transaction alone is not a snapshot guarantee: Neo4j's default
read-committed isolation permits
[non-repeatable reads](https://neo4j.com/docs/operations-manual/current/database-internals/concurrent-data-access/).
The engine therefore captures the tenant corpus revision and active embedding
generation before recall, guards every vector, BM25, graph, adjacency, and
hydration statement with that identity, and reads the identity again after the
last data statement. A successful result was evaluated while that identity
remained unchanged and its trace names that exact revision and generation. If
publication, deletion, an access-policy change, or an embedding cutover occurs
mid-pipeline, the engine discards all intermediate rows and retries once in a
new read transaction. A second change fails closed as retrieval unavailable;
no mixed-version result or stale authorization trace is returned. Real-Neo4j
integration tests place deterministic barriers between recall stages and
concurrently publish a new Version or revoke access.

Embedding generations and their managed Neo4j vector indexes still define and
audit vector-space coverage and atomic lifecycle cutover. The production
retrieval path nevertheless uses exact authorized cosine for correctness and
existence-signal resistance. An approximate alternative would require a
prefilter design plus comparative security, quality, and performance evidence;
bounded overfetch followed by authorization is not equivalent.

## Security and lifecycle boundary

Every vector, BM25, graph-expansion, adjacency, and hydration query requires:

- the request Principal's exact tenant;
- an intersecting group on both Document and Chunk;
- matching Document/Chunk access-policy identity and version;
- the Document's `ACTIVE_SNAPSHOT` and `ACTIVE_VERSION`;
- Snapshot membership and `OF_VERSION` linkage;
- caller-supplied Document, Version, and publication-time filters; and
- accepted Entity governance state for graph navigation.

BM25 uses `graphrag_chunk_text_v2`, whose indexed properties are `text` and
`retrieval_scope`. An active Chunk's scope contains an active marker plus
SHA-256-derived tenant and access-group tokens. The Lucene query requires the
active marker, exact tenant token, and at least one Principal-group token before
the bounded full-text candidate window is selected. Retiring a Version removes
the scope property. Raw tenant and group values are not placed in the Lucene
query. Migration 005 creates this v2 index; the older text-only v1 index remains
for migration safety but production retrieval never queries it.

Those partition tokens reduce the global index to an authorized candidate
partition; they are not the source of authorization truth. The same database
query still rechecks tenant, Document and Chunk ACLs, active Snapshot/Version,
access-policy identity, and requested Version filters before returning a hit.
The BM25 scan and returned rank are independently bounded. Graph degrees are
calculated only from Chunks visible to the same Principal, so protected
connectivity cannot influence the returned RA trace.

The required active, tenant, and group clauses use Lucene zero boosts. They
therefore constrain the candidate set without contributing to the BM25 score;
only the `text` clause determines relevance. A real-Neo4j integration check
uses equal text with different ACL-list lengths and requires identical scores.
The production-reference workflow pins the validated Neo4j patch release, so
an upgrade must re-run this parser/scoring compatibility check.

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
and unauthorized exposure count. Stage 8 connects this metric code to the
unified automated evaluation workflow. The Stage 9 workflow retains the exact
authorized-cosine and partitioned-BM25 invariants while measuring the fixed
production-reference scale and concurrency envelope.
