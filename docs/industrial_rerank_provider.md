# Industrial rerank provider boundary

`retrieval/rerank_provider.py` provides an explicit profile factory for Beijing
DashScope `qwen3-rerank` and `qwen3.8-max`. The selected industrial profile is
`listwise-evidence-v1`; changing profiles is an explicit experiment, never a
fallback. Neither implementation retrieves sources or decides access.
The caller supplies complete, currently authorized `RerankCandidate` objects;
the engine must recheck source versions, permissions, and publication identity
after the provider returns. Model calls occur outside a Neo4j read transaction.

The pointwise `qwen3-rerank` endpoint is
`https://dashscope.aliyuncs.com/compatible-api/v1/reranks`, as specified by the
[official rerank endpoint documentation](https://platform.qianwenai.com/docs/api-reference/rerank/openai-rerank).
This path differs from the existing embedding `compatible-mode` path. The
client does not migrate regions, replace models, retry, or fall back on failure.
The configured model is a provider alias; no immutable weights version is
claimed. A successful account-specific smoke test is separate from these unit
tests.

## Explicit profiles

- `listwise-evidence-v1`: the selected generic evidence-ranking instruction,
  frozen before holdout evaluation. `qwen3.8-max` receives every authorized
  candidate in one request and returns a zero-based permutation. The endpoint
  is `https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions`, with
  `temperature=0`, `max_tokens=1024`, `enable_thinking=false`, and
  `response_format={"type":"json_object"}`. The system instruction SHA-256 is
  `8c7f6167680ead2d380a4f624631f7b1e958ef9ec180c0a558616cb4aaa51f74`.
  It considers requested observations, necessary conditions, faithful
  alternative summaries, contradictions, and unavailable facts. Selection
  does not claim the retrieval acceptance targets have passed.
- `listwise-contextual-v1`: the separately identified historical listwise
  development profile, with its own instruction and cache identity.
- `default-qa-v1`: the wire request omits `instruct`; document inputs equal the
  original Chunk texts. This remains the factory default; industrial requests
  select `listwise-evidence-v1` explicitly.
- `contextual-qa-v1`: a fixed general instruction requests evidence for every
  question part, including contradictions, missing information, and source
  applicability. Each input is rendered as `Source document: {title}`, then
  `Source section: {section}`, then `Passage:` and the intact original text,
  separated by newlines. This profile is a development experiment, not a claim
  that the retrieval acceptance targets pass.

Both listwise profiles use the same intact title/section/body rendering as
`contextual-qa-v1`. The method follows the published
[RankGPT permutation-ranking approach](https://github.com/sunnweiwei/RankGPT),
adapted to one bounded list with the stricter parser below. This does not
reproduce its full sliding-window implementation. The configured model remains
the `qwen3.8-max` alias; a dated snapshot listed on the
[official model page](https://help.aliyun.com/zh/model-studio/qwen3-8-max)
is not silently substituted or claimed as the observed weights version.

`RerankProviderProfile` also permits an explicit bounded instruction for an
independently recorded experiment. Such configurations receive distinct hashed
profile identities. Applications should allow only their reviewed profile IDs.
The provider never infers an asset or appends scope to the query. Any explicit
user scope added by the request adapter becomes part of the exact input hash.

Rendered titles and section names are ranking context, not substituted source
evidence. The response records raw-source and rendered-input hashes separately
in original candidate order. Citation text, Chunk IDs, and source offsets remain
the original values.

## Bounded execution

Each request contains at most 50 whole candidates. Each query or rendered
document is limited to 3,600 UTF-8 bytes. For pointwise `qwen3-rerank`, an
explicit instruction is limited to 2,000 bytes and the conservative repeated-input budget is
`N * (query_bytes + instruction_bytes) + sum(document_bytes) <= 90,000`.
These are local **byte** limits, not a tokenizer or measured token counts.
Exceeding a limit fails the request; texts are not truncated.

Listwise additionally limits the entire canonical HTTP body to 90,000 UTF-8
bytes and the system message to 4,000 bytes. Its conservative repeated-query
budget excludes the once-sent system message, which remains included in the
whole-body limit. Both ranking methods retain the 50-candidate limit.

The official `qwen3-rerank` service documents 4,000 tokens per query/document
and 120,000 per request. Official pages currently differ on whether overlong
items are rejected or truncated, so the client does not rely on either
behavior. Service usage is recorded from `usage.total_tokens` rather than
estimated from characters. See the
[official API reference](https://help.aliyun.com/zh/model-studio/text-rerank-api).

One process-wide lock allows one in-flight client call and rejects concurrent
calls immediately. A deployment with multiple application processes must retain
an overall concurrency limit of one. Each production call starts a private
Python subprocess; the key and request travel on stdin, not argv or logs. The
parent applies a maximum 30-second deadline to `communicate`, covering blocked
stdin, connection, writes, and response reads. On timeout it kills and reaps the
child. Cleanup and OS scheduling can add small elapsed time after the deadline;
no timed-out thread or network worker continues in the background.

The child uses TLS verification, disables redirects and HTTP retries, and
streams at most 256 KiB of uncompressed response. Errors expose only fixed
categories. Connection/write timeouts are 5 seconds; read timeouts are 10
seconds for pointwise and 25 seconds for listwise, inside the unchanged
30-second parent deadline.
The injectable transport exists for tests; production uses the terminating
subprocess transport.

## Output and cache integrity

The pointwise provider requests all `N` scores and requires a complete permutation of
indices `0..N-1`: no duplicates, missing rows, booleans as indices, or nonfinite
scores. Scores must be in `[0,1]` and nonincreasing. The response model must
match, and a bounded request ID and actual nonnegative token usage are required.
Provider tie order is retained; any engine tie policy is recorded separately.

Listwise returns pure rank: every `RerankScore.score` is `None`, and the engine
preserves provider order without invented probabilities. A completed response
must have `finish_reason="stop"`, one assistant choice, and a JSON object
containing only `ranking`. The original list must have between `N` and `2N`
valid integer indices. Stable duplicate removal is the only normalization,
and the resulting list must cover every candidate exactly once. Missing indices
are never appended; booleans, out-of-range indices, truncation, and extra
explanations fail. The original permutation, method, and exact removed count
remain visible in the trace.

`rerank_cache_identity` binds model, endpoint, instruction, rendering version,
exact query, ordered Chunk IDs, source hashes, document/version identity, and
rendered text hashes. The exact canonical HTTP body has a separate SHA-256 hash.
For pointwise, `output_checksum` hashes **canonical normalized provider output** (model,
request ID, ordered indices/scores, and total token usage), not raw HTTP JSON
formatting. `validate_cached_response` reconstructs both input and output hashes
and checks the complete input/index binding. The caller must authorize candidates
before cache lookup; a cache cannot grant access.

For listwise, `raw_output_checksum` hashes the exact original response bytes;
the normalized output hash additionally binds that raw checksum, both
permutations, duplicate count, and actual usage. Its private cache must retain
the successful raw JSON and pass it to `validate_cached_response`.
`rerank_with_raw` supplies those bytes. Raw provider bodies stay outside the
public response and trace. A hit is revalidated against the actual query,
currently authorized whole candidates, rendered metadata, fixed profile, and
original response. It is not a new model call or a new token charge.

`response_metadata` exports actual usage and byte counts independently.
Pointwise reports observed `total_tokens` only; no prompt/output breakdown is
invented. Listwise records observed `prompt_tokens`, `completion_tokens`, and
`total_tokens` separately and requires consistent arithmetic. Completion
tokens are never counted as input. As checked on 2026-09-07, the published
Beijing list price for pointwise `qwen3-rerank` is CNY 0.5 per million input
tokens with no output charge. An estimate may use actual reported tokens
and a separately dated price snapshot; discounts and free quotas must not be
assumed. The authoritative billing source remains the account's bill and the
[official model pricing page](https://help.aliyun.com/zh/model-studio/model-pricing).
For `qwen3.8-max`, the published Beijing price checked on 2026-09-07 is CNY 12
per million input tokens and CNY 36 per million output tokens. Its separately
listed cache-hit input rate applies only when actual usage identifies cached
input; this implementation does not infer it from repeated queries. See the
[official model pricing details](https://help.aliyun.com/zh/model-studio/qwen3-8-max).

## Verification

```sh
.venv/bin/python -m unittest tests.unit.test_rerank_provider tests.unit.test_listwise_provider -v
```

The suite makes no model calls. It exercises a real killed/reaped subprocess
with blocked stdin, streamed HTTP limits through a mock transport, no-redirect
and no-retry behavior, intact text and contextual profile identity, cache
tampering, complete score permutations, byte limits, and concurrent rejection.
Listwise cases additionally check fixed messages and parameters, stable
complete duplicate normalization, rejection of missing or invented indices,
original-payload verification, observed token accounting, and the actual
25-second read timeout inside the unchanged parent deadline.
