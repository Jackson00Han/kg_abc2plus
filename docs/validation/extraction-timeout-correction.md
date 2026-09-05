# Extraction timeout correction

Date: 2026-09-05. Scope: the local, provider-backed property-graph construction
path, not answer generation or production-scale model-quality qualification.

## Cause and controlled observations

The configured model was `qwen3.8-max`. The old request omitted
`enable_thinking`, used non-streaming completion, `max_tokens=2048`, no SDK
retries and a 30-second provider timeout. Workflow/API budgets were 90/105
seconds. The original direct `APITimeoutError` at 31.289 seconds was the provider
deadline, not an API deadline or Neo4j query failure.

Alibaba documents that this model defaults to thinking and that `max_tokens`
limits the final answer, not reasoning. Non-streaming completion waits for
both: [thinking modes](https://help.aliyun.com/zh/model-studio/deep-thinking),
[request parameters](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions).

Three bounded live calls used the existing endpoint/key/model, the same
three-sentence ZX-47/Delta equipment source and the same industrial T-Box. No
database writes or credentials/reasoning-text logging occurred. The first two
used an identical prompt (7,218 characters; SHA256
`949c387f2bf95f8e0a6d123499fe9a53d5f14e557e943e6da0aad4ff1905e3f2`).

| Call | Observation | Result |
| --- | --- | --- |
| Default thinking, streaming for diagnosis only | TCP 0.415s, TLS 1.629s, headers 3.162s, first reasoning 3.172s | At the 30s wall bound: 3,440 reasoning characters, zero final-content characters |
| Thinking disabled, original non-streaming prompt | Headers 8.216s; complete 8.230s; 1,786 input + 377 output tokens | Finished normally, but exact-span validation rejected incorrect model offsets |
| Thinking disabled plus token-position hints | Complete 6.912s; 2,583 input + 361 output tokens | Three entities, two relationships and one property fact passed unchanged validation as SECONDARY candidates |

The measurements identify time spent generating reasoning in this reproduction,
not a bad key or a connection that never opened. They do not prove all network
latency is zero or guarantee future provider latency. Streaming was an
observation technique, not the shipped fix. The third call changes the prompt,
so its timing is not an isolated thinking-mode comparison.

## Changes and safety boundaries

- Explicit non-thinking extraction in the local DashScope factory, retaining
  the user's model/key, 30-second timeout, output cap and zero SDK retries.
- Optional mechanical Unicode token spans in the prompt. These are character
  coordinates only, not prewritten entity/relationship proposals. Work/size are
  linear in the already bounded source Chunk. The validator still rejects
  mismatches; no fuzzy matching, offset repair or automatic trust promotion.
- Safe SDK timeout classification: `MODEL_CALL_TIMEOUT`, HTTP 504, recoverable
  `RETRY_WAIT`. Ordinary provider failures remain HTTP 503. Completed Chunks
  survive retries without repeat model calls.
- Actual model, prompt, limits, response mode, seed, thinking and token-hint
  policy enter the extraction-profile identity before job replay. Changed
  policies require a new operation key; source versions and historical audit
  data remain immutable. Legacy injected extractors retain their identities.
- The workbench exposes the configured non-thinking policy. It still requires
  explicit review and explicit publication; approval does not make extracted
  knowledge authoritative.

## Repeatable checks

```sh
uv run --locked python -m unittest \
  tests.unit.test_construction_extraction \
  tests.unit.test_construction_workflow \
  tests.unit.test_construction_provider_errors \
  tests.unit.test_api_knowledge tests.unit.test_playground
```

112 focused tests passed. Tests cover explicit/default provider options, invalid
option types, Unicode/repeated-text coordinates, unchanged rejection of wrong
evidence, request-policy identity and replay, actual SDK timeout types, safe
HTTP classification, and resuming without re-extracting completed Chunks.

For a fresh live run that preserves an already running disposable Playground:

```sh
PLAYGROUND_BOLT_PORT=17693 ./scripts/run_playground.sh --port 8001 --no-open
```

Import/publish the industrial property-graph T-Box template under a new key,
upload a fresh equipment source, run automatic extraction, inspect the exact
candidate quotes/coordinates, then explicitly approve and publish. Check the
SECONDARY-inclusive graph and AUTHORITATIVE_ONLY graph separately. No manual
expert imports can substitute for the model phase. Provider calls incur usage.

## Corrected live workflow

A separate fresh service on `127.0.0.1:8001` ran the final code, preserving the
existing `8000` service and its data. An earlier unexposed test instance was
stopped during initialization to reload the final CJK hint correction; only
its disposable fixture database was removed, and it was rebuilt. The configured
model/key and all construction limits were unchanged. Bootstrap reports
`enable_thinking: false` and `span_hints: unicode-token-spans-v1`.

The fresh source (not the A/B input) was:

> Validation Pump ZX-58 is installed at Validation Site Kappa. Validation Asset
> Services operates Validation Pump ZX-58. The rated power of Validation Pump
> ZX-58 is 13 kW.

`POST /v1/knowledge:construct` returned HTTP 200 in **43.415 seconds**, including
source ingestion, Embedding, extraction and index-generation refresh. This is
not a measurement of model latency alone and must not be compared as such with
the 6.912-second direct provider probe. Job
`4cd7f50b-6077-58c6-9ac0-ba7f6860ddb9` completed one Chunk with no findings and
without replaying a prior extraction. The actual output contained three
entities, five distinct source mentions, two relationship assertions and the
`RatedPower=13 kW` typed property assertion. All eight governed records started
as `LLM_EXTRACTED / SECONDARY / CANDIDATE`.

The validating agent individually inspected the returned records against this
synthetic source, then exercised the explicit reviewer and publication APIs
using a test persona. This is a workflow test, **not domain-expert certification**
or user approval of production knowledge. No model outputs were rewritten,
replaced with expert imports or automatically promoted to authoritative status.

All 22 live checks passed, including:

- Exact document/version/Chunk identity and every quote/character range.
- Candidate visibility in the review queue but absence from the active graph.
- Approved-but-unpublished records still absent from the active inventory.
- Explicit publication of only the eight reviewed records; authority remains
  SECONDARY after review and publication.
- Source-linked entities, relationships and typed literals in the published
secondary-inclusive subgraph; absent from `AUTHORITATIVE_ONLY` output.
- Tenant Beta receives 404 for the Alpha job and no source Chunk/subgraph.
- The published graph-quality report passes.

To inspect these local test records, select **Tenant Alpha · Finance + Legal**
and the SECONDARY-inclusive graph policy. The synthetic publication is
`e67a949d-8b3a-54cf-b525-23154bbf873e`, bound to T-Box
`extraction-correction-20260905-zx58`. It is deliberately not visible to the
public-only or other-tenant persona.

The first read-only inspection harness incorrectly applied Python-mode strict
datetime validation to decoded HTTP JSON. It was corrected to
`model_validate_json`; inspection resumed without another model call or any
candidate mutation. The service's original construction response was successful.

The service is a disposable development Playground. These small-source results
do not establish industrial extraction precision, large-document throughput,
or a guarantee that all future provider calls finish within 30 seconds. The
existing exact-evidence checks, bounded failure handling and expert review
remain required.

## Full regression and reviewed baseline

The complete development workflow passed **824 tests** with no failures,
errors or skips: 649 unit, 15 HTTP E2E, 33 security, two regression and 125
real-Neo4j integration tests. The unit suite was rerun after the final CJK
correction (165.170 seconds), and the final report uses that frozen-code
evidence. The integration suite took 765.834 seconds and its disposable
container was removed. Corpus/gold rebuild, contract validation, lock check,
wheel/sdist build, Python compilation, shell syntax and whitespace checks passed.

Baseline 1.4.0 was independently and manually compared with 1.3.0: its only
deterministic projection change is 13 additional unit test IDs. Every prior
test ID, all 160 per-case digests, 20 contract metrics, diagnostics, migrations
and locked extraction-quality identities remain unchanged. No threshold or
security requirement was relaxed. The baseline-version maintenance passed
19 focused evaluation tests. Two report replays over the same final suite and
observation inputs passed and reproduced semantic digest
`2afca5d7ffab8deec085eed41925f602c6f4d2fbf8e0ee4226d2d3c93ec9dba6`.
These are report replays, not two independent integration cycles. This remains
development-scale evidence and does not renew Stage 9 qualification.
