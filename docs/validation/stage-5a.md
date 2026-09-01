# Stage 5A Validation Record

- Date: 2026-09-02
- Acceptance contract: `contracts/acceptance.v1.json` version 1.0.1
- Validation profile: `dev-mini`
- Development corpus: `dev-corpus-v1` version 1.0.1
- Python: 3.12.12
- Database fixture: `neo4j:5.26.12-community`
- Image digest: `sha256:9f75e8df4325a24f00fdd7a8c0bcce650a58375049b1058e496e8b43d6c36b37`

## Delivered

- A deterministic 10-document, 120-Chunk synthetic development corpus across
  two tenants, five company identities, two fiscal years, and five access
  groups.
- Exact source files, immutable IDs/checksums, contiguous character ranges,
  adjudicated entities/relationships, 49 seven-class questions, and 128-
  dimensional fixture vectors.
- A reproducible builder whose `--check` mode detects missing, extra, or
  byte-different committed files without modifying the checkout.
- A real-Neo4j corpus loader and regression suite covering actual ingestion,
  complete embedding generations, ranking metrics, final context, citations,
  tenant/access isolation, and deterministic refusal gating.
- Version-aware exact-content deduplication and a Chunk-neutral extraction
  artifact encoding order.
- Correct `[0, 1]` validation and calibration for Neo4j cosine-similarity
  scores.

## Reproducible checks

Run from the repository root:

```bash
uv run --locked python scripts/build_dev_corpus.py --check
env -u OPENAI_API_KEY -u OPENAI_BASE_URL \
  -u NEO4J_URI -u NEO4J_PASSWORD \
  uv run --locked python -m unittest discover \
  -s tests/unit -p 'test_*.py' -v
./scripts/run_stage5a_neo4j_tests.sh
uv run --locked python scripts/evaluate_retrieval.py
python3 scripts/validate_acceptance_contract.py
uv lock --check
uv build --out-dir /tmp/sample-graphrag-stage5a-dist
uv run --locked python -m compileall -q src tests scripts
sh -n scripts/run_stage2_neo4j_tests.sh
sh -n scripts/run_stage3_neo4j_tests.sh
sh -n scripts/run_stage4_neo4j_tests.sh
sh -n scripts/run_stage5_neo4j_tests.sh
sh -n scripts/run_stage5a_neo4j_tests.sh
git diff --check
```

The disposable runner removes provider credentials, refuses a pre-existing
fixed-name container, binds Bolt to loopback only, applies all migrations to a
clean database, and removes the container on exit. It retains the `dev-mini`
cap of 1.5 GiB, one CPU, a 512 MiB heap, and a 128 MiB page cache.

## Verified behavior

- All 99 offline unit tests pass.
- All 60 disposable-Neo4j integration tests pass in 391.038 seconds.
- The committed builder reproduces exactly 10 sources, 120 Chunks, 19 governed
  entities, 49 questions, and 169 fixture vectors.
- Every Chunk round-trips to its immutable source range and contains its
  canonical company name, ticker, and fiscal year.
- All 120 vectors and vector checksums round-trip through Neo4j exactly; both
  tenant embedding generations have complete active coverage.
- Actual final-ranking metrics are Recall@5 1.0, MRR 1.0, nDCG@5 0.987956,
  with zero unauthorized exposures.
- Actual selected-context diagnostics are Recall@5 1.0, MRR 1.0, nDCG@5
  0.969938, with zero unauthorized exposures.
- Stable citations reproduce the exact Document Version, Chunk checksum,
  ordinal, character range, and source text.
- Cross-tenant and wrong-group evidence remains absent from recall, graph
  expansion, candidate ranking, adjacency, final ranking, and selected context.

## Defects exposed and resolved

The first multi-document ingestion exposed an artifact-cache conflict that the
three-Chunk fixture could not trigger. Byte-identical extraction inputs were
encoded in an order derived from Chunk-specific mention/assertion UUIDs. The
codec now orders portable relative records by Chunk-neutral semantic fields,
and a regression test proves that one payload safely rebinds to distinct stable
Chunk IDs.

Initial actual retrieval measured Recall@5 0.657143, MRR 0.558095, and nDCG@5
0.547002. Investigation found incomplete evidence labels, Chunks that were not
self-identifying outside document context, provenance-erasing cross-Version
content deduplication, and a score-domain mismatch. Neo4j cosine similarity is
mapped to `[0, 1]`, where orthogonal vectors score `0.5`; a raw-cosine-style
floor of `0.01` therefore admitted noise. The final fixture uses an audited
`0.75` Neo4j-score gate: relevant vectors score at least 0.853553 and unrelated
orthogonal vectors score 0.5. RRF and RA formulas were not changed, and the
acceptance thresholds were not weakened.

## Decision and limitations

Stage 5A satisfies its exit criteria under `dev-mini`: at least 100
representative Chunks rebuild from a clean checkout, real ingestion and
retrieval remain bounded and permission-safe, and actual ranking/context
quality exceeds the Stage 1 thresholds.

The filings and evidence-cluster vectors are explicitly synthetic. Their
manifest declares `quality_claim: none`; they validate data lifecycle,
retrieval orchestration, provenance, and authorization, not an external
embedding model, real filing distribution, customer corpus, or production
scale. Production-reference 10,000-Chunk, provider, concurrency, and sustained
load evidence remains required in Stage 8 and Stage 9.
