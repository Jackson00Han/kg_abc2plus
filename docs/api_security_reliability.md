# API, Security, Reliability, and Observability

Stage 7 exposes the Stage 3, 5, and 6 application capabilities through a
strict HTTP boundary.  It does not create a module-level application, read
credentials on import, or choose an embedding/model provider.  Deployment
assembly must inject those trusted resources into `create_app`.

## HTTP surface

| Method | Path | Purpose | Success |
| --- | --- | --- | --- |
| `POST` | `/v1/documents:ingest` | Synchronously ingest one authoritative UTF-8 source version | `200` |
| `DELETE` | `/v1/documents/{document_id}` | Synchronously delete one tenant-scoped document lifecycle | `200` |
| `GET` | `/v1/jobs/{job_id}` | Read a tenant-scoped durable job projection | `200` |
| `POST` | `/v1/retrieval` | Return traceable Chunks and optional governed graph context | `200` |
| `POST` | `/v1/answers` | Retrieve and generate a grounded cited answer | `200` |
| `POST` | `/v1/knowledge/authoritative:import` | Import expert A-Box records against the active published T-Box | `200` |
| `POST` | `/v1/knowledge:construct` | Upload one bounded source and create review-gated LLM extraction candidates | `200` |
| `GET` | `/v1/knowledge/review-queue` | Read bounded candidate/quarantine revisions | `200` |
| `POST` | `/v1/knowledge/reviews:batch` | Apply compare-and-swap expert review decisions and raw-source edits | `200` |
| `GET` | `/health/live` | Check only the local process boundary | `200` |
| `GET` | `/health/ready` | Check bounded dependency readiness | `200` or `503` |
| `GET` | `/v1/metrics` | Read aggregate operational metrics | `200` |

All non-health application endpoints require one Bearer token.  Metrics also
requires both the configured system tenant and observer group.  OpenAPI is
generated from the same Pydantic request and response models.

## Trusted identity and authorization

`JWTAuthenticator` fixes the algorithm to HS256 in deployment code; it never
uses an untrusted JWT header to choose the algorithm or key.  It verifies the
signature, issuer, strict audience, expiry, issued-at time, subject, tenant,
groups, maximum token size, and maximum lifetime.  Authentication failures
collapse to one public message.

The controller constructs a domain `Principal` exclusively from verified
claims.  Retrieval and answer requests cannot contain `tenant_id`,
`principal_id`, `access_groups`, `query_vector`, or an embedding-space ID.
`GraphRAGQueryOperations` obtains the query vector and exact space ID from the
server-owned embedder.  It rejects a retrieval result whose trace tenant,
space, limits, version filter, or selected Chunk IDs differs from that trusted
request.

Retrieval enables governed graph projection by default with
`include_graph=true`. `graph_trust_policy` is either
`PUBLISHED_SECONDARY_INCLUSIVE` (the default) or `AUTHORITATIVE_ONLY`; it is a
trust filter, never an ACL override. The optional projector receives only the
authenticated `Principal` and the retrieval trace's exact selected Chunk IDs.
When no projector is configured, or the caller explicitly disables it, the
response contains `graph: null` to distinguish unavailable/disabled projection
from a successfully projected empty graph. Projector timeouts and malformed or
cross-tenant results fail closed without exposing dependency details.

Ingestion contains an ACL because it creates a new resource.  The requested
groups must be a non-empty subset of the authenticated principal's groups.
The controller and application backend both check this before provider or
database work.  `DocumentOperations` receives the trusted `Principal` for
ingestion, deletion, and job lookup.  Implementations must scope database
queries by `principal.tenant_id` and return the same `404` for a cross-tenant
identifier and an unknown identifier; the HTTP boundary cannot add a tenant
after an unsafe lookup.

Knowledge construction follows the same non-delegation rule. Its
`access_groups` field is mandatory, unique, non-empty, and must be a subset of
the verified JWT groups. The application adapter and workflow both enforce the
subset. Only the selected groups are placed on the Document, Chunks,
construction outcomes, and governed evidence; a principal that also belongs
to a broad group cannot accidentally widen a restricted upload to that group.
Existing sources cannot have their ACL silently changed through this endpoint.

Every retrieval data path continues to apply the Stage 5 tenant, group,
active-version, and optional version filters inside Neo4j.  The API does not
post-filter results.  Graph information remains navigation data; only exact
source Chunks are returned as factual context.
The optional evidence-subgraph projection reuses the retrieval trace's exact
Document, Version, and publication-time filter for both seeds and expanded
assertions; it cannot widen the caller's retrieval boundary.

## Validation and output integrity

All request models reject unknown fields, booleans in numeric fields,
non-finite numbers, duplicate identifiers, naive timestamps, overlong text,
and limits outside server caps.  Standard JSON arrays and ISO-8601 timestamps
are accepted explicitly without enabling broad scalar coercion.  The complete
request body is capped before JSON parsing, including chunked bodies; source
content itself is limited to 5 MiB of UTF-8.

Response contracts are also fail closed.  Retrieval text must match its exact
character range and SHA-256 Chunk checksum.  Boundary whitespace is preserved
verbatim rather than normalized, and the returned Chunk order must equal the
trace selection.  Bounded retrieval explanations allow the maximum legal
Resource Allocation evidence list without making trace text unbounded.  Answer
responses preserve the Stage 6 standard refusal and require every material
claim, server-owned citation, inline marker, inference label, and conflict
index to be internally consistent.  Invalid backend output becomes a generic
dependency error instead of leaking a validation traceback.

The graph response is independently bounded and omits tenant and ACL fields.
Every Entity mention, relationship assertion, literal assertion, and one-hop
path carries an exact Chunk citation, exact quoted span, publication and
ontology IDs, origin, authority, `PUBLISHED` status, and confidence. Entity and
assertion references must be internally consistent, and the response-level
Chunk/publication manifests must exactly match their evidence. Graph context
remains navigation metadata: `/v1/answers` continues to send only retrieved
Chunks to grounded generation.

Literal assertions and their one-hop paths also expose optional
`literal_semantics`: declared datatype, parsed value, exact raw value/unit,
canonical value/unit, UTC validity/observation instants, and their exact raw
temporal tokens. The field is `null` only for readable historical legacy
records that predate typed literals. New authoritative imports and review edits
accept raw source tokens only; unknown/canonical client fields are rejected and
the server recomputes semantics from the assertion's active published T-Box.
Ontology imports reject units that Pint cannot parse before writing a draft and
return `422 invalid_request`; this is semantic request validation, not a `409`
compare-and-swap conflict. Publication, rollback, and history payloads expose
the immutable `ontology_version_id` used to validate that publication.

## Bounded execution and retry rules

Knowledge construction applies a second, operation-specific bound before any
embedding, ingestion, or extraction provider work. The parsed source must fit
configured maximums for Chunks, potential model calls, and total extraction
characters. Server configuration cannot raise those limits above the module
ceilings of 512 Chunks, 512 model calls, 5 MiB of extraction text, or a
900-second cooperative deadline. A monotonic workflow deadline is checked
between stages and before every model call, and the extractor's single-call
timeout must be shorter than the workflow deadline. A request that cannot fit
starts no additional provider call: static budget violations are
`invalid_request`, while an elapsed cooperative deadline is
`dependency_timeout`.

Synchronous provider/database work runs in a fixed thread pool with a hard
capacity of `max_workers + max_queue_size`.  Submission is non-blocking when
that capacity is full.  A timeout or cancelled HTTP coroutine does not release
the capacity permit while its underlying thread is still running; only the
worker future's completion callback does so.  This prevents an accumulation of
untracked timed-out work.

API-level automatic retries are restricted to explicitly marked retryable
failures from provider-free job-status and health/readiness reads.  Retrieval
is attempted once at this boundary because it first invokes the embedding
provider; its Neo4j managed read transaction may still use the driver's
bounded retry policy without repeating the embedding call. The retrieval
engine also performs at most one new read transaction when its captured corpus
revision or embedding generation changes mid-pipeline; it discards the first
transaction's rows and reuses the already validated query embedding. A second
change fails closed. See `docs/production_retrieval.md` for the guarded
linearization contract. Ingestion, deletion, and answer generation are also
attempted once. Writes already use their Stage 3 idempotency keys and durable
jobs; answer generation is not retried because it can duplicate billable model
calls. Timeouts and unclassified exceptions are never retried.

A successful ingestion or deletion response is `200`, contains a terminal
`SUCCEEDED` or `NOOP` job in the `COMPLETE` phase, and therefore does not imply
that work was merely queued.  There is no in-process background queue behind
these endpoints.

A timed-out write may still finish in its worker thread.  Because the timeout
response cannot safely claim a job identifier before the synchronous backend
returns, clients must retry the exact request after bounded backoff with the
same operation key to resolve the outcome.  The durable idempotency fingerprint
returns the existing
terminal result when the first attempt completed and rejects reuse of the key
with different input.  Once a response supplies its job identifier, the
job-status endpoint can be used for later audit.  Clients must not substitute a
new operation key merely because the HTTP response timed out.

Application shutdown first stops new work and closes the rate limiter, then
invokes bounded resource-close callbacks.
It does not wait without limit for synchronous provider threads; closing their
owned resources interrupts remaining I/O while completion callbacks retain
correct capacity accounting.

`Neo4jSettings` validates credential-free Neo4j/Bolt URIs, database and user
names, secret bounds, connection-pool size, acquisition/connect timeouts,
connection lifetime, and driver transaction retry time.  `Neo4jResource`
uses managed read/write transactions and maps known retryable driver failures
to one redacted dependency error.  Retrieval has an independently configurable
server transaction deadline (60 seconds by default, at most 300) so the
one-CPU `dev-mini` corpus remains bounded without weakening authorization or
quality checks.  Its representation and probes never expose the username,
password, URI error, or server response.

## Rate limits and errors

The default rate limiter is a bounded, thread-safe in-process token bucket
keyed by a collision-safe tenant/subject composite.  A fixed-window policy is
also available.  Accepted calls receive limit, remaining, and reset headers;
rejected calls receive `429` and an integer `Retry-After`.

The public error taxonomy is stable:

- `invalid_request`, `unauthenticated`, `forbidden`, `not_found`, `conflict`;
- `rate_limited`, `dependency_timeout`, `dependency_unavailable`;
- `overloaded`, `runtime_closed`, and `internal_error`.

Errors return only the code, a fixed public message, and request ID.  Input
values, provider exceptions, decoded claims, database addresses, and
validation details are excluded.  Every response has server-generated trace
and request IDs, `Cache-Control: no-store`, and
`X-Content-Type-Options: nosniff`.  A caller request ID is accepted only when
it is a single bounded safe identifier.

## Logs and metrics

`StructuredJsonLogger` emits one JSON object per line from a fixed field
allowlist: event, level, service, timestamp, request/trace ID, route template,
method, status, error code, and duration.  It cannot log a request body,
question, source text, prompt, answer, Chunk, citation, token, or raw
exception through this interface.  Recursive redaction additionally removes
credential-shaped keys and values, JWTs, private keys, URI userinfo, and
control-character injection.

`MetricsRegistry` stores only bounded-cardinality aggregates:

- request/error counts and latency buckets by method and route template;
- error counts by stable code;
- operational-stage latency, including embedding, retrieval, optional graph
  projection, and generation;
- model-call, input/output-token, and estimated-cost totals.

Unknown paths collapse to one route label and excess labels collapse to an
overflow bucket.  No tenant, principal, document, query, model response, or
source identifier is a metric label.  Providers must report real token and
cost usage in `UsageMetadata`; zero means unavailable, not free.

## Assembly boundary

A deployment creates, validates, and owns secrets and resources outside the
package import path, then assembles:

1. `JWTAuthenticator` from a secret manager value and approved issuer/audience;
2. `Neo4jResource` with explicit pool and timeout settings;
3. deployment-specific `DocumentOperations`, query embedder, and answer model;
4. `GraphRAGQueryOperations` and `GraphRAGApplicationBackend`;
5. `create_app`, passing resource close callbacks.

The repository intentionally does not provide environment-driven global app
construction.  It would otherwise make imports open connections, obscure
secret ownership, and silently select provider or graph-pipeline versions.

Implementation choices were checked against the official FastAPI lifespan
documentation, the current Neo4j Python driver API, and the PyJWT API:

- <https://fastapi.tiangolo.com/advanced/events/>
- <https://neo4j.com/docs/api/python-driver/current/api.html>
- <https://pyjwt.readthedocs.io/en/stable/api.html>

## Known deployment boundaries

- HS256 key distribution and rotation remain deployment responsibilities; a
  production identity platform may require an asymmetric/JWKS verifier.
- The in-process rate limiter is per process.  A multi-replica deployment
  needs a shared gateway or limiter with the same semantic policy.
- Stage 7 verifies usage-accounting transport with deterministic providers.
  The Stage 9 reference envelope measures deterministic token and cost
  accounting, but real external-provider tokenization, pricing, latency,
  availability, quota, and retention behavior remain deployment checks.
- Development runners use the configured Neo4j tag. The Stage 9 qualification
  runner instead pulls and runs the committed repository digest directly,
  verifies its RepoDigest without changing any pre-existing local tag, and
  records the observed source and restored container resource envelopes.
- `dev-mini` API and Neo4j results are functional/security evidence only and
  cannot qualify the production-reference workload.
