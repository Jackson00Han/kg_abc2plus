# Local GraphRAG Retrieval and Knowledge-Governance Playground

The local Playground is a one-command, browser-based way to exercise the
validated GraphRAG retrieval and governed property-graph construction paths. It
runs against a new disposable Neo4j container, loads the committed
`dev-corpus-v1`, and sends every action through the real authenticated `/v1`
API. It remains a retrieval service: it does not generate a final answer.

## Run it

Requirements:

- Python 3.12 or newer
- `uv`
- Docker with at least 1.5 GiB available
- an Alibaba Cloud Model Studio API key with access to `text-embedding-v4` and
  the selected Qwen extraction model

Configure the OpenAI-compatible embedding endpoint in the untracked `.env`:

```dotenv
OPENAI_API_KEY=replace-with-a-current-key
OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_MODEL=text-embedding-v4
EMBEDDING_DIMENSIONS=1024
MODEL_NAME=qwen-plus
```

`EMBEDDING_MODEL` is used for document/query vectors. `MODEL_NAME` is used only
for ontology-constrained entity, relationship, and typed-property extraction;
it is never used to write a final answer. Never commit `.env` or paste an API
key into source code, logs, screenshots, or chat. The browser receives only
provider/model capability metadata and never receives the credential.

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

Startup includes provider calls to embed all 120 Chunks, so its duration depends
on provider latency and quota. Press Ctrl-C to stop the API. The exact disposable
container is then stopped and removed; no database volume is retained. The
launcher refuses non-loopback API and Neo4j addresses. It reads the
OpenAI-compatible settings from `.env` and uses them server-side for embeddings
and governed extraction. Retrieval transactions remain bounded at 30 seconds.
During startup warm-up only, a retrieval-store timeout permits one retry of the
same read with the same vector and authorization. Embedding is not repeated;
a second timeout or any other error still prevents the service from starting.
Local construction is preflighted at four Chunks, four model calls, and 16,000
extraction characters, then cooperatively capped at 90 seconds inside a
105-second API deadline. Answer
generation is deliberately unavailable. Extraction uses a separate no-retry
provider client with a 30-second per-call timeout, a 2,048-token output cap,
and provider-neutral response-format mode. The compact output shape remains in
the prompt and the server strictly validates JSON, evidence, and the T-Box; a
provider timeout is returned as a bounded dependency failure rather than
silently retrying costly model calls.

Model availability does not imply that extraction finishes within this local
budget. The 2026-09-05 live `qwen3.8-max` single-Chunk acceptance timed out at
30 seconds; its recoverable job and the downstream manual-governance checks
are documented in
[`validation/governance-workbench-completion.md`](validation/governance-workbench-completion.md).
That report keeps real-provider limitations separate from passing deterministic
regression tests.

## What the page exercises

The initial example and the 49 reviewed questions execute the complete local
retrieval path:

```text
short-lived JWT -> tenant/access-group authorization
  -> provider query embedding -> vector + BM25 recall
  -> RRF fusion -> bounded Resource Allocation graph expansion
  -> gating/deduplication/adjacent context
  -> structured Chunks, provenance, Retrieval Trace, and authorized subgraph
```

The page shows:

- seven synthetic test identities across two tenants and their access groups;
- reviewed retrieval cases including conflict, unanswerable, and unauthorized scenarios;
- selected source text with document version and exact Chunk character range;
- the published, trust-aware one-hop knowledge subgraph connected to selected
  Chunks, including entities, relationship/literal assertions, exact evidence,
  authority, origin, status, and confidence;
- Vector, BM25, RRF, bounded graph expansion, candidate cosine ranking, and
  final-ranking traces;
- an advanced retrieval panel for document/version cutoffs, every bounded
  retrieval limit, graph inclusion, and authoritative-only graph projection;
- evidence-backed entity-resolution suggestions in the human-review queue,
  including exact `identity_properties` matches and atomic dependent-fact
  rebinding after expert confirmation;
- typed relationship-property values, their exact evidence spans, and visible
  relationship-property and endpoint-cardinality T-Box contracts;
- recoverable construction-job progress and bounded Chunk-outcome detail,
  immutable per-record revision history, and selectable publication candidates;
- an independently authorized active-publication quality card showing the
  exact T-Box/publication binding, graph digest, bounded counts, pass state,
  issue metadata, and deterministic review sample Chunk IDs without source text;
- explicit immutable quality-audit recording, a bounded publication-filtered
  history list, and historical report details separated from the live report;
- an independently authorized active A-Box inventory showing the exact active
  publication/T-Box binding, entity and assertion structure, trust metadata,
  typed relationship properties, and exact evidence locations without source
  text; it can be filtered by Document ID and can map selected revision entries
  to stable record IDs for a subsequent publication removal;
- an independently authorized active-document lifecycle card showing bounded
  source metadata, ACL, Chunk count, active snapshot/generation CAS values, and
  stable governance blocker codes, with explicit logical retirement only when
  no blocker remains;
- the unmodified retrieval JSON returned by the production API.

Switching to an identity other than a question's recommended identity lets a
developer verify that recall, graph expansion, adjacent context, and returned
Chunks remain inside that identity's tenant and access groups.

The original fixture graph predates the governed publication layer. It remains
useful for retrieval/RA tests, but its entities do not masquerade as reviewed
knowledge. Subgraph results appear for instances that have passed the governed
import or extraction, review, and publication flow.

## Knowledge-construction workbench

Use the **知识构建** view for the complete property-graph governance loop:

1. Edit and import the visible default industrial T-Box JSON. This creates a
   draft only; a human must explicitly publish it. Existing versions can be
   loaded with their checksum for exact replay validation, copied to the next
   unused version for editing, or downloaded as a self-describing JSON
   artifact. Each starter entity type declares both its expert-managed
   namespace and the explicit
   `llm-candidate` provisional namespace required for reviewable model
   proposals. Approval never silently promotes that namespace to expert truth;
   authority and origin remain separate governed fields.
2. Optionally import expert-controlled A-Box records. Every record must bind to
   an active document version and an exact, ACL-authorized Chunk substring.
   Typed literal assertions accept only source-owned `raw_literal`, optional
   raw unit, and optional raw temporal strings; datatype parsing, unit
   canonicalization, canonical values, and normalized timestamps are generated
   by the server from the published T-Box and are never trusted from a client.
3. Upload one UTF-8 `txt`, `md`, `csv`, or `json` document (maximum 5 MiB),
   select a non-empty subset of the current persona's access groups plus a
   published T-Box key, and run construction. The UI defaults to one narrow
   group rather than silently broadening document visibility. The server parses
   and chunks the document, embeds each Chunk in the configured vector space,
   and asks the configured LLM for T-Box-constrained proposals. The page shows
   the server-advertised Chunk/model-call/deadline cost boundaries when present.
   The 5 MiB transport limit is not a promise that a large file will pass these
   tighter local construction budgets; oversize parsed workloads are rejected
   before extraction calls begin.
   Failed submissions retain one browser-session operation key for the exact
   file bytes, metadata, identity, T-Box, and selected ACL. Retrying unchanged
   input therefore resumes the same durable job; a successful response or any
   input change rotates the key. The task panel can recover recent visible jobs
   and inspect their bounded durable outcomes.
4. Inspect each Chunk's findings and each candidate/quarantined record. A human
   may approve, reject, quarantine, or submit a strict JSON edit, individually
   or in a batch. Each card can also load the stable record's immutable revision
   history, including review status, actor, time, and notes where present.
5. Refresh recoverable publication candidates and select current approved or
   previously published-but-inactive revisions. Replacement candidates are
   identified and sent with the required logical record IDs. Manual revision
   IDs remain available for expert workflows. A separate record-ID input can
   remove references from the active publication, including a removal-only
   change set. IDs are deduplicated and cannot be removed and replaced in the
   same request. This is an auditable publication change; it does not directly
   delete the source document or immutable record revisions. View publication history and use
   CAS-protected rollback when required. History shows the exact published
   T-Box version so schema lineage remains visible.
6. Inspect the active A-Box inventory as a full steward. The dedicated
   `knowledge:quality` scope and complete visibility over every revision in the
   active publication are required. The server validates the complete manifest
   before applying the optional Document ID filter or bounded 1–500 item limit.
   Each item shows kind, trust/origin/authority/confidence, the entity or
   assertion structure, typed relationship-property values, and exact
   document/version/Chunk/character evidence locations without evidence text.
   Revision history can be opened through the existing ACL-safe endpoint.
   Selecting inventory revisions and clicking the removal action copies their
   deduplicated stable **record IDs**—not revision IDs—into the step 5
   publication-removal input. Refreshing the inventory (including invalid
   parameters), changing identity, or submitting a publication/rollback clears
   the prior inventory snapshot, its selection/history, and removal IDs added
   by this handoff; manually entered removal IDs are preserved during refresh.
   Request sequencing prevents older inventory or revision-history responses
   from restoring stale selections after a newer request or publication change.
7. Run the active graph quality audit as a full steward. The dedicated
   `knowledge:quality` scope is required, complete publication ACL visibility is
   mandatory, and missing/conflicted active state is shown without leaking
   graph contents. The card contains only bounded metadata and Chunk IDs.
   **运行质量审计（不保存）** and opening the page remain read-only.
   **运行并保存审计** explicitly records an immutable observation, including
   failed reports. Repeating an identical run preserves its first observer and
   timestamp. The separate history list returns up to 10 visible runs and can
   filter by publication ID; its details are labelled historical observations
   and never replace the current live report. Identity changes and request
   sequencing discard stale responses. Forbidden, missing, conflicting, and
   unavailable states have distinct messages.
8. Refresh active documents as a full steward. The dedicated
   `knowledge:lifecycle` scope is required. A document referenced by an active
   publication, current review revision, or in-flight job shows a stable
   blocker and no retirement button. After resolving blockers, explicit browser
   confirmation submits the active snapshot, source generation, and a
   same-input session operation key. Success rebuilds and activates the tenant's
   vector generation before the API returns; immutable source and audit records
   remain available for governance.
9. Retrieve the uploaded content. Only records in the active publication that
   remain bound to current authorized evidence appear in **知识子图**.

Expert imports are marked `AUTHORITATIVE`; LLM-extracted records are marked
`SECONDARY` and cannot enter the active graph until review. Graph data is
navigation context. The exact Chunk remains factual evidence. Review cards and
the retrieval subgraph display the complete optional `literal_semantics`
projection (raw and canonical forms); legacy records may legitimately return
that projection as `null`.
Relationship review cards and retrieved graph edges likewise show typed
relationship-property values with exact supporting spans. Publication checks
their property and endpoint cardinality against the complete final manifest.

Local identities demonstrate separation of duties rather than giving every
persona administrator access:

| Identity group | Local scopes |
| --- | --- |
| `alpha-public`, `beta-public` | retrieval and ontology read |
| `alpha-finance` | read plus knowledge construction |
| `alpha-legal` | read plus knowledge review |
| `alpha-finance + alpha-legal` | full local ontology/knowledge governance, including active graph quality and document lifecycle |
| any `beta-board` identity | full local ontology/knowledge governance, including active graph quality and document lifecycle |

The page shows the selected persona's scopes. A `403` is an expected,
non-leaking result when a persona attempts a duty it does not hold.

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

- It supports bounded document upload and calls a chat-capable model only for
  ontology-constrained extraction. Returned Chunks and subgraph evidence are
  intended for a downstream model or orchestration layer.
- It does not generate final answers.
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
custom hybrid query, graph-response contract, ontology-list authorization,
answer-scope denial, changed-tenant isolation, and readiness. The provider run
is evaluated against the Gold annotations and
requires MRR and fractional evidence Recall@5 of at least 0.80 for both final
ranking and selected context, with zero unauthorized exposure. These are local
provider smoke gates, not the stricter production-reference Recall@5 and
nDCG@5 acceptance targets. The command prints the measured values so model or
endpoint changes remain visible. It exits with a
non-zero status on any mismatch and removes its container when complete.

Normal serving also performs the same preload and warm-up before it starts the
API and page. This makes the first browser query representative of the warm
local runtime instead of Neo4j's initial query-plan compilation.

The four corpus counters in the right-hand inspector are explicitly labelled
as the immutable **startup fixture baseline**. They describe the committed
`dev-corpus-v1` input (10 Documents, 120 Chunks, 19 fixture Entities, and two
Tenants); they are not relabelled as live state after an upload or retirement.
The active-document card instead calculates a fresh Documents/Chunks summary
from the bounded lifecycle response for sources that are completely visible to
the selected JWT identity. An authorization or dependency failure is displayed
as unavailable and never falls back to the fixture counters.

`/health/ready` is also retrieval-aware. In addition to reaching Neo4j, it
requires exactly the expected Tenant corpus-state rows, exactly one active
embedding generation and pointer per Tenant, equality between each generation
and corpus revision, and an `ONLINE` Neo4j vector index for every active
generation. Missing or duplicate state, a stale revision, a missing/offline
index, or a driver failure returns `not_ready`. The response exposes only the
safe `ok`/`error` checks `neo4j`, `embedding_generations`, and `vector_indexes`;
it never includes Tenant IDs, index names, connection details, or exception
text.
