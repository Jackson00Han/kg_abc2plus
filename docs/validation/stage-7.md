# Stage 7 Validation Record

- Date: 2026-09-02
- Acceptance contract: `contracts/acceptance.v1.json` version 1.0.1
- Validation profile: `dev-mini`
- API package version: 0.1.0
- Python: 3.12.12
- Database fixture: `neo4j:5.26.12-community`
- Recorded image digest:
  `sha256:9f75e8df4325a24f00fdd7a8c0bcce650a58375049b1058e496e8b43d6c36b37`

The Stage 7 runner invokes the database image by tag.  It does not resolve or
enforce the recorded digest at runtime.

## Delivered

- Strict ingestion, deletion, durable-job, retrieval, grounded-answer,
  liveness, readiness, and metrics HTTP contracts.
- Fixed-algorithm JWT authentication and server-owned tenant, principal,
  group, vector, and embedding-space boundaries.
- Tenant/ACL checks before work submission and again in the application
  backend; cross-tenant identifiers use the same public not-found response as
  unknown identifiers.
- Fixed-worker/queue execution, timeouts, read-only retry policy, bounded
  per-principal rate limits, deterministic errors, and lifecycle shutdown.
- Validated Neo4j URI, pool, timeout, managed-transaction, readiness, and
  secret-safe resource handling.
- Protected-content-safe structured logs and bounded-cardinality request,
  error, latency, retrieval-stage, token, model-call, and cost metrics.
- Real-Neo4j API integration through JWT, server-side fixture embedding,
  Stage 5 retrieval, Stage 6 generation, stable citations, and unauthorized
  negative cases without an external provider.

## Reproducible checks

Run from the repository root:

```bash
uv run --locked python -m unittest discover -s tests/unit -p 'test_*.py' -v
uv run --locked python -m unittest discover -s tests/e2e -p 'test_*.py' -v
uv run --locked python -m unittest discover -s tests/security -p 'test_*.py' -v
./scripts/run_stage7_neo4j_tests.sh
uv run --locked python scripts/build_dev_corpus.py --check
python3 scripts/validate_acceptance_contract.py
uv lock --check
uv build --out-dir /tmp/sample-graphrag-stage7-dist
uv run --locked python -m compileall -q src tests scripts
sh -n scripts/run_stage2_neo4j_tests.sh
sh -n scripts/run_stage3_neo4j_tests.sh
sh -n scripts/run_stage4_neo4j_tests.sh
sh -n scripts/run_stage5_neo4j_tests.sh
sh -n scripts/run_stage5a_neo4j_tests.sh
sh -n scripts/run_stage6_neo4j_tests.sh
sh -n scripts/run_stage7_neo4j_tests.sh
git diff --check
```

The disposable runner removes provider and ambient Neo4j credentials, refuses
an existing fixed-name container, binds Bolt to loopback port 17693, starts an
empty database, and removes the container on exit.  It retains the `dev-mini`
cap of 1.5 GiB, one CPU, a 512 MiB maximum heap, and a 128 MiB page cache.

## Verified behavior

- All 248 offline unit tests pass.
- All four HTTP end-to-end tests pass and cover every endpoint, trusted JWT
  envelopes, response headers, rate limiting, readiness failure, metrics, and
  shutdown.
- All eleven adversarial security tests pass, including token attacks, client
  identity/vector injection, ACL escalation, no-echo validation, request-ID
  injection, declared/chunked body limits, log leakage, cross-tenant existence
  probing, metrics authorization, and route-cardinality attacks.
- All 64 disposable-Neo4j integration tests pass in 360.386 seconds from a
  clean database under the `dev-mini` CPU and memory cap.
- The real-Neo4j HTTP path returns stable exact Chunk text, checksums, ranges,
  citations, and traces through authenticated retrieval and grounded answer
  generation.  Server-generated vectors and JWT-derived tenant/groups are the
  only values reaching the domain request.
- Stage 5A regression metrics remain Recall@5 1.0, MRR 1.0, nDCG@5
  0.9879560689802693, with zero unauthorized exposure.  Stage 6 supported
  claim, citation, numerical-fidelity, refusal, correctness, and temporal
  comparison rates remain 1.0, with zero generation failures or forbidden
  answer exposure.
- The clean run exposed and the final suite covers two evidence-chain
  regressions: response normalization of exact Chunk boundary whitespace and a
  too-short legal RA explanation field.  It also covers Neo4j's client-config
  transaction-timeout code and a 60-second bounded retrieval deadline suitable
  for the one-CPU validation profile.

## Decision and limitations

Stage 7 satisfies its functional exit criteria under `dev-mini`: requests are
authenticated and authorized, tenant-safe, strictly validated, rate- and
concurrency-bounded, observable without protected content, and deterministic
when dependencies fail or exceed deadlines.  The API, security, reliability,
and real-Neo4j checks above are repeatable from committed commands.

The current evidence uses deterministic embeddings and answer payloads and an
in-process API client.  It validates authorization propagation, behavior,
failure bounds, observability exclusions, and real Neo4j integration, but not
a public network edge, distributed rate limiter, external identity service,
real provider token/cost reporting, or production-reference load.  Those are
not silently treated as passing; Stages 8 and 9 retain the corresponding
evaluation and production-candidate gates.
