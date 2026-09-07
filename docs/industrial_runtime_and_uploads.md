# Industrial runtime and governed uploads

The industrial workbench is an opt-in assembly of the existing authenticated
API. Start an **already initialized** local database with:

```sh
uv run python scripts/run_playground.py --enable-industrial \
  --reuse-existing-corpus --no-open --port 8002 --skip-provider-warmup
```

The last flag skips provider warm-up only; actual retrieval and extraction use
the configured external providers. Startup checks the published industrial
ontology, completed corpus load, matching active embedding space and current
coverage, and the active graph publication. It does not load seed data, replace
publications, or create an embedding generation. The generic full-database
Playground reset is disabled whenever industrial mode is enabled. Existing
personas 01–07 retain their identities, scopes, questions and retrieval engine.
The readiness query is restricted to the explicitly enabled tenant set; the
bootstrap reports that set, including `industrial-schneider-demo`.

Industrial personas 08, 09, 10 and 11 represent a public reader, engineer,
maintenance uploader, and knowledge administrator. Persona 11 is the initial
workbench selection. All can read the authorized graph; reader access does not
grant lifecycle, quality, import or publication authority. Maintenance can
construct; a separate administrator performs review and publication. Assets
are obtained from authorized APIs, never an unauthenticated complete inventory.
Using different uploader and reviewer identities is the demonstrated workflow;
the generic review API records the reviewer but does not enforce
`uploader != reviewer` as a server-side separation-of-duties rule.

For the industrial tenant the server supplies an industrial scope even when a
client omits it, and routes only that tenant to the locked
`listwise-evidence-v1` reranker. The default industrial retrieval limits match
the I3 evaluated profile, including 50 candidates, five anchors, top five chunks
and the existing 12,000-character budget. Explicit alternate bounded limits
remain visible in the trace. No evaluation cache or fixture provider is used by
the running service. Other tenants keep the existing retrieval behavior.

## Upload contract

Use the existing `POST /v1/knowledge:construct` request, with:

```json
{
  "canonical_uri": "industrial-upload://industrial-schneider-demo/my-inspection-001",
  "tbox_key": "industrial-electric-v1",
  "access_groups": ["maintenance"],
  "industrial_context": {
    "family": "canalis-kt",
    "asset_keys": ["asset-bkt-a01"]
  }
}
```

The remaining existing fields (operation key, title, source name, MIME type,
base64 content and extraction mode) are unchanged. Family is required and must
be `canalis-kt` or `evopact-hvx-up24`. Asset keys are optional, canonical,
unique and bounded to eight. A selected asset must already occur in an
explicit primary-asset facet of an authorized seed field source in that family.
Each selected reader group must be able to see it, so an upload cannot expose a
protected asset identifier by adding `public` to its ACL. Family-only uploads
may omit asset keys. Text mentions do not silently expand applicability.

The server fixes origin to `USER_UPLOAD` / `USER_PROVIDED_NOT_VERIFIED`.
Clients cannot submit authority, source-kind, actor or tenant overrides inside
the context. The immutable job identity includes the context and authenticated
actor; changing either under the same operation key conflicts. Industrial seed
URIs are protected by the separate upload namespace. Other tenants cannot
submit industrial context. Existing nonindustrial request identities remain
unchanged.

After the normal source snapshot is published, a construct-only provenance
write verifies the job, exact request fingerprint, source version/checksums,
current snapshot and complete ACL in one transaction. It then stores canonical
metadata and derived facets atomically on that immutable DocumentVersion. This
path does not grant `knowledge:import`. A provenance interruption cannot report
a successful completed job; exact replay completes it without re-embedding the
already published source. Missing facets exclude a partial source from the
industrial search scope until recovery. Metadata already attached to a version
cannot be reassigned to another context or operation.

`SOURCE_ONLY` records audited evidence and makes no extraction calls. `LLM`
uses the active composed industrial TBox and creates governed candidates only
when extraction validation permits them;
existing review/publication rules still apply and approved extracted knowledge
remains secondary. Publishing an accepted new batch uses the existing complete
manifest and compare-and-swap path, preserving the seed records. A user upload
never automatically publishes graph facts or becomes company-approved.

## Extraction outcomes and recovery

Source publication precedes extraction. A completed construction response with
a `REJECTED` chunk can therefore have a current, searchable source and zero
candidate records. Its job status `COMPLETED` means processing reached a
terminal result, not that extraction succeeded. The workbench displays source
ingestion separately from candidate counts and per-chunk outcomes. Rejected
extraction, provider failure, empty extraction and deliberate `SOURCE_ONLY`
remain distinct; quarantined records are identified separately. Only actual
`CANDIDATE` records receive guidance to proceed to independent review and, after
approval, publication. A network error alone does not establish whether source
publication completed: inspect the retained task first.

The local runtime uses prompt version
`industrial-property-graph-extraction:v6-exact-json-spans` and requests
`response_format={"type":"json_object"}`. This does not certify that returned
facts or source spans are valid. The existing budgets remain unchanged: at most
two LLM-mode chunks, two validation attempts per chunk, four model calls per
construction, and a 30-second limit per call. `max_attempts` in the construction
request controls ingestion attempts, not the separate validation-correction
budget. No terminal result triggers an automatic fresh construction request.

Within the same browser tab and identity, an unchanged file and form metadata
reuse the saved operation key and source URI. An exact replay of a completed
rejection reads the original outcome; it is not a new model run. The UI retains
that operation identity after rejection and uncertain network errors. Changing
the extractor profile under the same key conflicts with the original request
fingerprint. The API does not offer an in-place re-extraction operation.

For an explicit development verification of a corrected profile, use a new
operation key **and a new source URI**, retaining the original job, source and
immutable validation artifacts. A new operation with the same URI and bytes
is insufficient: the existing version's industrial provenance is bound to its
original construction job and request fingerprint and cannot be reassigned.
Do not describe the new run as repair of the old job. This workbench adds no
new re-extraction button and does not silently duplicate or remove sources.

An administrator with `knowledge:lifecycle` may explicitly withdraw a failed
test source from active retrieval through the existing document lifecycle UI
or `GET /v1/knowledge/documents` followed by
`POST /v1/knowledge/documents/{document_id}:retire`. The latter uses a fresh
operation key and the inventory's current snapshot ID and source generation.
Existing publication, review and running-job blockers still apply. Logical
retirement retains source versions, chunks, construction jobs and extraction
audits; it is not physical deletion. The maintenance persona lacks this
capability, and `/industrial` has no new retirement control.

## Interrupted validation recovery

If the model response and its validation attempts were durably stored but
parent assembly was interrupted, resubmitting the original request can recover
that complete chain without another model call. Current source permissions and
lifecycle, artifact identity and checksum, request scope, predecessor links and
strict response validation must all still pass. Final rejection remains a
rejection. A valid incomplete provider-failed chain permits a fresh attempt
within the existing request budget; damaged or competing chains fail closed.

Legacy attempt artifacts have no separate parent lookup fields. Recovery
therefore validates a complete, bounded set for the current tenant, audit kind
and extraction profile before selecting the requested chain. It does not use
JSON whitespace or property ordering to decide whether an attempt exists.
This compatibility path is limited to 256 artifacts, eight MiB per artifact,
32 MiB in aggregate and 32 attempts for one chain. Exceeding a bound requires
an explicitly designed index migration; it never silently starts another model
call. These are recovery limits, separate from corpus and graph-view limits.

## Authorized source reading

`POST /v1/industrial/sources:query` lists at most 100 authorized current sources
(default 50). It returns source identity, family, explicit assets, source kind,
publication date, authorized chunk count, first chunk, and `has_more`; there are
no hidden-source counts. Canonical source URIs are identifiers, not a promise
that a browser can download every original. Official evidence contains its
pinned publisher links in the chunk provenance.

`POST /v1/industrial/sources:chunk` reads a current chunk, exact text/checksum,
ranges, physical PDF locations when available, and authorized previous/next
chunk IDs. SOURCE_ONLY uploads need no graph publication to be readable.
Unknown and inaccessible chunks both return no chunk. Current Document,
DocumentVersion, published source snapshot, Chunk ACL and immutable provenance
must agree. A final source/target recheck also rejects ACL or location repairs
that did not advance the corpus revision; the target text must equal the exact
substring of its immutable version. A scope or corpus change during the read
fails explicitly. Publication times are rechecked as exact epoch seconds plus
nanoseconds, avoiding driver timezone round-trip equality differences without
rounding away a change in the source timestamp.

Source reads retain the 100-source cap, two-second database-query bound,
15-second cooperative operation deadline, eight-MiB individual provenance
limit, 50,000-character chunk output limit and 512-KiB per-chunk location output
limit. Maps are decoded one source at a time. These are interactive development
limits; narrow the scope when the corpus exceeds the bounded inventory.

Online uploads still support text, Markdown, CSV and JSON with the existing
five-MiB byte, four-chunk and bounded extraction limits. Industrial uploads use
900-character gapless chunks; other tenants retain the original splitter.
Before a job, embedding or extraction call, the server validates each industrial
chunk with the actual locked contextual renderer, including title, section and
selected asset context. An oversized rendered UTF-8 input is rejected with a
specific 422 input-limit error; shorten the title or split the document. Unicode
text is preserved exactly, never truncated to fit. This check uses no provider
calls and does not change the I3 model, prompt or retrieval budgets. Query length
and the combined candidate-pool limits remain checked when retrieval runs.
Long blank padding that creates an entirely blank evidence chunk is rejected
with the existing 422 invalid-request response before source writes; it is not
silently removed from the source.
Uploads do not introduce
online PDF upload or OCR. Official PDF normalization stays in the independently
bounded offline acquisition/normalization process. Physical table maps do not
reconstruct merged-cell headers or technical applicability; manual adjacent
reading does not change the locked retrieval evaluation.

## Verification

```sh
uv run python -m unittest tests.unit.test_industrial_runtime \
  tests.unit.test_industrial_upload_budget \
  tests.security.test_industrial_upload_security
```

`tests.integration.test_industrial_upload_neo4j` runs only against an explicitly
opted-in empty disposable Neo4j. It exercises real source-only ingestion,
replay without repeated embeddings, current ACL isolation, immutable source
facets, adjacent reading, preserved seed publication, and recovery after a
provenance interruption. Chinese source text is checked against exact chunk
ranges, and final reads reject ACL, location or single-nanosecond timestamp
changes between queries. The full industrial runner and complete existing
regression suites remain required for the milestone report.
