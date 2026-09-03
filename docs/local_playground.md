# Local GraphRAG Retrieval Playground

The local Playground is a one-command, browser-based way to exercise the
validated GraphRAG retrieval core. It runs against a new disposable Neo4j
container, loads the committed `dev-corpus-v1`, and sends every query through
the real authenticated `/v1/retrieval` API.

## Run it

Requirements:

- Python 3.12 or newer
- `uv`
- Docker with at least 1.5 GiB available
- an Alibaba Cloud Model Studio API key with access to `text-embedding-v4`

Configure the OpenAI-compatible embedding endpoint in the untracked `.env`:

```dotenv
OPENAI_API_KEY=replace-with-a-current-key
OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_MODEL=text-embedding-v4
EMBEDDING_DIMENSIONS=1024
```

`MODEL_NAME` is not used by the Playground. Never commit `.env` or paste an API
key into source code, logs, screenshots, or chat.

From the repository root:

```bash
./scripts/run_playground.sh
```

The script opens <http://127.0.0.1:8000/playground>. Use `--no-open` when a
browser should not be opened automatically:

```bash
./scripts/run_playground.sh --no-open --port 8080
```

Set `PLAYGROUND_BOLT_PORT` only when the default local Neo4j port `17692` is
already occupied:

```bash
PLAYGROUND_BOLT_PORT=17693 ./scripts/run_playground.sh
```

Startup now includes provider calls to embed all 120 Chunks, so its duration
depends on provider latency and quota. Press Ctrl-C to stop the API. The exact disposable container is then stopped
and removed; no database volume is retained. The launcher refuses non-loopback
API and Neo4j addresses. It reads the OpenAI-compatible embedding settings from
`.env` and uses them only for document and query embeddings.
Retrieval transactions and API work remain bounded at 30 and 45 seconds
respectively. Playground JWTs contain only the `retrieval:read` scope; answer
generation is deliberately unavailable.

## What the page exercises

The initial example and the 49 reviewed questions execute the complete local
retrieval path:

```text
short-lived JWT -> tenant/access-group authorization
  -> provider query embedding -> vector + BM25 recall
  -> RRF fusion -> bounded Resource Allocation graph expansion
  -> gating/deduplication/adjacent context
  -> structured Chunks, provenance, and Retrieval Trace
```

The page shows:

- seven synthetic test identities across two tenants and their access groups;
- reviewed retrieval cases including conflict, unanswerable, and unauthorized scenarios;
- selected source text with document version and exact Chunk character range;
- Vector, BM25, RRF, graph expansion, reranking, and final-ranking traces;
- the unmodified retrieval JSON returned by the production API.

Switching to an identity other than a question's recommended identity lets a
developer verify that recall, graph expansion, adjacent context, and returned
Chunks remain inside that identity's tenant and access groups.

## Two query modes

| Mode | Behavior | Intended use |
| --- | --- | --- |
| Reviewed question | Real provider Vector + BM25 + RRF + graph retrieval | Reproduce validated retrieval cases with semantic recall |
| Custom text | The same provider Vector + BM25 + RRF + graph pipeline | Explore arbitrary natural-language questions over the corpus |

At startup the Playground embeds all 120 Chunks with the configured external
model, builds a matching Neo4j vector-index generation, and activates it per
tenant. Every query uses that same model and embedding-space identity. The
committed deterministic vectors remain test fixtures but are not activated by
the running Playground.

The local UI uses a deliberately bounded graph profile: three fused seeds,
eight entities and at most 40 graph edges per seed, eight retained graph
candidates per seed, and 50 total candidates. This caps the graph-expansion
Trace at 24 candidate occurrences before stable deduplication. The production
retrieval contract retains its independently versioned defaults.

## Boundaries

This Playground proves that the local reference implementation is wired and
usable; it is not a live production deployment or a general-purpose chatbot.

- It does not upload documents. Arbitrary ingestion still needs concrete
  splitter, extractor, embedding, and lifecycle provider adapters.
- It does not generate answers or call a chat LLM. Returned Chunks are intended
  for a downstream model or orchestration layer.
- It calls the configured embedding provider during startup and for each query,
  so provider availability, quota, latency, data handling, and cost apply.
- It uses synthetic filing-like data only. The short-lived local JWTs grant
  access solely to that disposable database.

## Focused checks

```bash
uv run --locked python -m unittest tests.unit.test_playground -v
sh -n scripts/run_playground.sh
./scripts/run_playground.sh --check
```

The final command is the repeatable integration check: it builds a clean schema,
ingests all 120 Chunks, activates the tenant-specific embedding generations,
refreshes full-text indexes, and prewarms one bounded query per tenant before
exercising the authenticated retrieval path for all 49 reviewed cases, a
custom hybrid query, answer-scope denial, changed-tenant isolation, and
readiness. The provider run is evaluated against the Gold annotations and
requires MRR and fractional evidence Recall@5 of at least 0.80 for both final
ranking and selected context, with zero unauthorized exposure. These are local
provider smoke gates, not the stricter production-reference Recall@5 and
nDCG@5 acceptance targets. The command prints the measured values so model or
endpoint changes remain visible. It exits with a
non-zero status on any mismatch and removes its container when complete.

Normal serving also performs the same preload and warm-up before it starts the
API and page. This makes the first browser query representative of the warm
local runtime instead of Neo4j's initial query-plan compilation.
