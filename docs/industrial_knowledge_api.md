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

T-Box contracts can carry typed relationship-property definitions and declared
entity `identity_properties`, but the current A-Box API does not accept,
extract, review, or publish relationship-property values, and automatic entity
resolution does not yet use `identity_properties`. They are governance metadata
until those evidence-backed instance paths are implemented.

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
