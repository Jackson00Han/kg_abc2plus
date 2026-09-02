# Stage 5 Validation Record

- Date: 2026-09-01
- Acceptance contract: `contracts/acceptance.v1.json` version 1.0.1
- Retrieval dataset: `evaluation/retrieval-gold-v1.json` version 1.0.0
- Python: 3.12.12
- Database fixture: `neo4j:5.26.12-community`
- Image digest: `sha256:9f75e8df4325a24f00fdd7a8c0bcce650a58375049b1058e496e8b43d6c36b37`

## Delivered

- Reusable retrieval models, ranking primitives, metric implementations, and
  Neo4j orchestration under `graphrag_prod.retrieval`.
- Cosine vector and BM25 recall, standard RRF fusion, bounded RA graph
  expansion, candidate semantic re-ranking, and adjacent-Chunk completion.
- Deterministic score/channel gates, stable-ID and exact-content deduplication,
  whole-Chunk character budgeting, exact Version filters, and configurable
  limits for every candidate-producing stage.
- Query-time tenant, Document ACL, Chunk ACL, active Snapshot/Version, access
  policy, and governance checks on recall, graph expansion, adjacency, and
  final hydration.
- Stable citations and a structured deterministic retrieval trace.
- A versioned 49-case, seven-class retrieval regression fixture and metric CLI.
- Neo4j migration 004 with the versioned Chunk full-text index and retrieval
  scope index.
- Resource-bounded disposable Stage 5 integration runner.

## Reproducible checks

Run from the repository root:

```bash
env -u OPENAI_API_KEY -u OPENAI_BASE_URL \
  -u NEO4J_URI -u NEO4J_PASSWORD \
  uv run --locked python -m unittest discover \
  -s tests/unit -p 'test_*.py' -v

./scripts/run_stage5_neo4j_tests.sh
uv run --locked python scripts/evaluate_retrieval.py
python3 scripts/validate_acceptance_contract.py
uv lock --check
uv build --out-dir /tmp/sample-graphrag-stage5-dist
uv run --locked python -m compileall -q src tests scripts
sh -n scripts/run_stage2_neo4j_tests.sh
sh -n scripts/run_stage3_neo4j_tests.sh
sh -n scripts/run_stage4_neo4j_tests.sh
sh -n scripts/run_stage5_neo4j_tests.sh
git diff --check
```

The disposable runner refuses an existing fixed-name container, binds Bolt to
`127.0.0.1:17690`, removes provider credentials, applies all migrations twice,
runs every Neo4j integration test, and removes the container on exit. It uses
the versioned `dev-mini` cap of 1.5 GiB, one CPU, a 512 MiB heap, and a 128 MiB
page cache.

## Verified behavior

- 88 offline unit tests pass.
- 53 real-Neo4j integration tests pass.
- The 49-case retrieval fixture records Recall@5 1.0, MRR 1.0, nDCG@5 1.0,
  and zero unauthorized exposures, exceeding all Stage 1 retrieval/security
  targets.
- Real-Neo4j tests exercise every Stage 1 question class, including positive
  retrieval, graph navigation, exact values, retired/current Version conflict,
  insufficient-context gating, and authorization denial.
- Chunk-only denial, Document-only denial, cross-tenant isolation, protected
  RA neighbors, protected adjacency, and final hydration return no protected
  ID, score, evidence, citation, or existence signal.
- Every returned citation resolves to the exact immutable Document Version and
  Chunk character range.
- Repeating the same request against the same corpus state yields the same
  trace ID and ordering.
- A same-dimension query from a different embedding space fails closed instead
  of silently mixing vector generations.
- All Stage 2-4 schema, provenance, ingestion, recovery, embedding-generation,
  authorization, and graph-governance tests continue to pass.

## Decision and limitations

Stage 5 passes its exit criteria: retrieval is bounded, deterministic,
explainable, permission-safe, version-aware, and the committed regression
fixture exceeds the Stage 1 Recall@5, MRR, and nDCG@5 targets without an
unauthorized exposure.

This is `dev-mini` functional and quality evidence, not production-candidate
performance evidence. The 49-case fixture is a deterministic adjudicated
regression baseline; future customer-corpus adjudication and unified Stage 8
execution remain required. Exact authorized cosine recall favors security and
correctness over approximate-index speed. Stage 9 retains that invariant, adds
authorization-partitioned BM25 recall, and measures the design under the fixed
10,000-Chunk, eight-client reference envelope. Any approximate alternative is
separate future work and requires comparative security, quality, and
performance evidence before adoption.
