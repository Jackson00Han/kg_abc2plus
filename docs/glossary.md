# Acceptance Glossary

These definitions make the acceptance contract testable and prevent the same
metric from changing meaning between stages.

- **Accepted source**: a document version whose ingestion job succeeded, whose
  required metadata and checksum passed validation, and whose access policy is
  active.
- **Active version**: the one immutable version of a logical document selected
  for ordinary retrieval at a given time.
- **Answerable query**: a query for which the caller can access enough accepted
  source evidence to satisfy the complete requested answer.
- **Boundary case**: a negative or contrast case designed to expose false
  retrieval, false refusal, incorrect version choice, unsupported inference, or
  authorization bypass.
- **Citation coverage**: the fraction of material factual claims that have at
  least one valid supporting citation.
- **Citation precision**: the fraction of supplied citations that actually
  support the claim to which they are attached.
- **Conflict**: two accessible, currently valid sources make incompatible
  claims and the configured authority/version policy cannot select one.
- **Evidence**: exact text and location in an accepted source chunk. An entity
  name or graph edge without a source chunk is not evidence.
- **Gold case**: a versioned evaluation record with question, caller scope,
  expected behavior, expected stable evidence IDs, relevance grades, and an
  adjudicated answer or rubric.
- **Graph fact**: a typed assertion derived from a source, with subject, object,
  extraction version, confidence, and one or more evidence chunk IDs.
- **Ingestion success**: a terminal successful job for which schema, source,
  version, chunk, graph, index, and publication invariants all hold.
- **Material factual claim**: an independently verifiable entity, relationship,
  event, quantity, date, comparison, or causal statement in an answer.
- **Production candidate**: a version that passes the committed acceptance
  contract in a recorded reference environment. It is not automatically a live
  service or proof of fitness outside that environment.
- **Retrieval latency**: server-side time from validated retrieval request to a
  bounded context result; answer-model time is excluded and reported
  separately.
- **Stable ID**: an application identifier whose value survives database
  export, restore, and graph rebuild when its identity inputs are unchanged.
- **Unauthorized exposure**: protected text, metadata, graph hints, scores,
  citations, identifiers, or existence signals visible to a caller outside the
  document policy.
- **Unanswerable query**: a query for which accessible accepted evidence is
  insufficient. General model knowledge does not make it answerable.
- **Validation complete**: every hard gate in the named contract version has a
  repeatable passing result, or an exception is explicitly recorded as not
  passed. It does not imply an operational availability SLO.

