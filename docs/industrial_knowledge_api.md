# Industrial knowledge governance API

The governed property-graph workflow is exposed through the same authenticated,
rate-limited, body-limited, and bounded worker runtime as the existing GraphRAG
API. The request contracts never accept a tenant ID, principal ID, capability,
or access-group override. These values come only from the verified JWT.

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
The construction workflow derives a new document ACL from the authenticated
principal and preserves the persisted ACL for updates.

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
