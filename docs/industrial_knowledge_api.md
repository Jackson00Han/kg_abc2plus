# Industrial knowledge governance API

The governed property-graph workflow is exposed through the same authenticated,
rate-limited, body-limited, and bounded worker runtime as the existing GraphRAG
API. The request contracts never accept a tenant ID, principal ID, or
capability override. These values come only from the verified JWT. A
construction request must explicitly select its source `access_groups`; every
selected group must be a non-empty subset of the verified JWT groups.

| Operation | Route | Required scope |
| --- | --- | --- |
| List T-Box versions | `GET /v1/ontologies` | `ontology:read` |
| Import a draft T-Box | `POST /v1/ontologies:import` | `ontology:write` |
| Publish a T-Box version | `POST /v1/ontologies/{id}:publish` | `ontology:publish` |
| Import authoritative A-Box | `POST /v1/knowledge/authoritative:import` | `knowledge:import` |
| Upload and construct candidates | `POST /v1/knowledge:construct` | `knowledge:construct` |
| Read the review queue | `GET /v1/knowledge/review-queue` | `knowledge:review` |
| Compute entity-resolution suggestions | `GET /v1/knowledge/entity-resolution/{record_id}` | `knowledge:review` |
| Apply one reviewed entity link | `POST /v1/knowledge/entity-resolution:apply` | `knowledge:review` |
| Submit review decisions | `POST /v1/knowledge/reviews:batch` | `knowledge:review` |
| Publish approved revisions | `POST /v1/knowledge/publications:publish` | `knowledge:publish` |
| Roll back a publication | `POST /v1/knowledge/publications/{id}:rollback` | `knowledge:publish` |
| Read publication history | `GET /v1/knowledge/publications` | `knowledge:publish` |

Upload content is canonical base64 and decodes to at most 5 MiB. Supported MIME
types are `text/plain`, `text/markdown`, `text/csv`, and `application/json`.
The construction workflow persists only the caller's selected group subset and
requires an exact ACL match on later updates; it never widens a source to all
groups held by a multi-group principal. Before embedding, ingestion, or LLM
work, the workflow rejects sources exceeding its configured Chunk, model-call,
or extraction-character budget. Module ceilings prevent configuration above
512 Chunks, 512 model calls, 5 MiB of extraction text, or a 900-second
cooperative deadline. Provider calls must expose a smaller per-call timeout.

T-Box relationship-property definitions are active A-Box contracts.
Authoritative imports and review edits accept raw-only property inputs; the
server generates stable value IDs and canonical datatype/unit/time values.
Each value supplies its own exact evidence and inherits the parent assertion's
tenant, document/version, Chunk, policy, and ACL. Extraction, review,
publication, retrieval, and graph responses preserve both raw/canonical
semantics and evidence. Publication also enforces relationship-property and
closed-world source/target cardinality over the complete final manifest;
bounded ACL-filtered retrieval views are intentionally not revalidated.

Declared entity `identity_properties` are active resolution keys. The
suggestion route reads the current candidate mention and its server-normalized
typed identity facts, searches only the caller's authorized evidence in the
active publication and exact T-Box version, and returns auditable
`AUTO_LINK`/`REVIEW`/`NO_MATCH`/`CONFLICT` outcomes. Apply requires the expected
candidate revision and one target returned by a freshly recomputed suggestion;
it atomically rebinds dependent candidate assertions while leaving those facts
unapproved. Unknown, stale, unauthorized, and cross-tenant targets do not
expose existence.

Model extraction uses only the system-reserved `llm-candidate` identity
namespace. Every extractable entity type must declare that namespace; an
incompatible T-Box or an alternate extractor namespace fails before provider
work begins. The persistence boundary verifies both origin and declaration:
model-derived identities cannot use expert namespaces, and authoritative or
other non-model identities cannot use the reserved candidate namespace.

Authoritative imports identify evidence only by document, immutable version,
Chunk, exact character range, and exact quoted text. The server resolves the
access-policy snapshot only through the document's active version and active,
published knowledge snapshot. The A-Box store revalidates that evidence inside
its write transaction. Expert records enter as
`EXPERT_IMPORT + AUTHORITATIVE + PUBLISHED`; model output enters only the
candidate or quarantine lanes until human review and explicit publication.

All mutations are non-retry-safe at the HTTP runner. T-Box publication,
knowledge review, and publication/rollback preserve their underlying CAS
preconditions. Unknown and cross-tenant IDs share the same public not-found
response.
