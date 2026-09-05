# Bounded Playground warm-up recovery

An observed cold Neo4j startup exceeded the existing 30-second retrieval
transaction deadline during initial bounded retrieval. Startup failed closed
and removed its temporary container before another warm-up read could be
attempted; the observation does not isolate query execution from compilation.

`scripts/run_playground.py::_warm_retrieval` now retries that tenant's read once
only when the engine raises `RetrievalBackendTimeout`. Both attempts use the
same request object, principal, ACL groups, retrieval limits, vector, and
embedding-space identity. Query embedding happens once per tenant, outside the
retry boundary. Each retrieval transaction retains its existing 30-second cap.

The second timeout and all other exceptions propagate. A successful retry must
still return the expected tenant and nonempty context before startup proceeds.
Provider failures are not retried through this database recovery path.

Focused verification:

```bash
uv run --locked python -m unittest \
  tests.unit.test_playground_warmup tests.unit.test_playground -v
```

The new checks cover timeout recovery, unchanged request/ACL/vector and one
embedding call per tenant, persistent timeout failure, non-timeout failure,
provider timeout, and unchanged readiness validation after a retry.
All 34 focused tests passed, including the five new warm-up checks.
