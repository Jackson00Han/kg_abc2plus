# Industrial retrieval gold

`datasets/industrial-v1/evaluation/gold.json` contains 72 source-annotated
questions: eight examples for each of the eight industrial question classes,
plus eight authorized controls paired with protected-record requests. The
36 development and 36 holdout cases are grouped by equipment event. Product
references and cross-asset identity passages are shared, so this is not an
unseen-document evaluation.

The corpus pin is
`f79af571b20364e08d2a966da129714b417b6445cd46f5744339697050d06703`.
The gold semantic checksum is stored in the gold file and repeated in its
manifest. It covers questions, source ranges, document checksums, relevance,
complete evidence alternatives, answerability, principal, supplied scope and
pairing. Moving the local file does not change its identity. The module exposes
`load_gold()`, immutable case records and a JSON-compatible `gold_to_dict()`.

Version 1.0.1 includes the final independent identity review: an additional
complete same-name distinction in case 003 and a necessary water-treatment
identity source in case 006. These additions were made without inspecting
predictions. Any result carrying the earlier 1.0.0 identity must retain that
version and be rerun against the final annotations.

## Annotation and independence

Questions and evidence were drafted from the authored corpus, then separately
reviewed for all 36 development and 36 holdout cases. Those reviews corrected
redundant evidence requirements, omitted valid summaries, mistaken time
premises and overly broad confidentiality claims. This was an AI-assisted
source review without retrieval predictions or provider calls; it is not
enterprise subject-matter-expert adjudication or evidence of real diagnosis
accuracy. The underlying field events remain explicitly synthetic.

Freeze the source annotations and their checksum before generating baseline
predictions. Prediction artifacts must record the gold, corpus, model, index
and runtime configuration versions independently. Retrieval code receives the
question, principal and declared user scope, not expected answers, relevant
anchors or answerability labels. Tune on development results only. A later
source-truth correction requires a documented gold version change and reruns;
failed predictions cannot justify changing the expected evidence.

## Source relevance and complete context

Every positive anchor is an exact `document_key#section_key`, with its original
character range and document checksum. The grading rubric is:

| Grade | Meaning |
| --- | --- |
| 3 | Direct detailed evidence for requested facts, original rows or a necessary condition. It may answer one part of a multipart question. |
| 2 | A faithful complete synthesis, or source evidence necessary to establish a requested relationship, condition or limitation. |
| 1 | Directly relevant partial evidence for an actual requested claim, such as one identity field or a sampling limitation. |
| 0 / omitted | Merely the same asset, a tangential topic, or text that does not support any requested claim or necessary condition. |

All grades above zero form the ordinary positive set for existing Recall@5,
MRR and nDCG calculations. Valid source alternatives remain positive even if a
different original section is preferred. There is no primary-only replacement
recall, alternative scoring formula or relaxed acceptance target.

`required_evidence_sets` serves a separate context-completeness check. Members
within one set are all needed; different sets are complete alternatives. For
example, the identity question can be supported by one complete registration
paragraph or by a location paragraph together with a component paragraph. The
two are not four mandatory citations. A paragraph repeating the requested
distance cannot substitute for the missing humidity in a two-quantity answer.

The manifest reports each case's theoretical Recall@5 ceiling before any run.
Some family-only identity and structural questions have more than five genuine
positive sections. At the initial freeze, the mean ceiling over positive-target
cases is approximately **0.9581 development**, **1.0000 holdout**, and **0.9790
overall**. All 72 cases, including the eight without an authorized positive
target, remain in the report. A ceiling is a mathematical bound, not measured
retrieval quality, and individual cases below the contract target are visible.

## Scope and answerability

The eight identity questions receive only family scope. Their expected asset
is not supplied to the retriever. Other questions model a user selecting an
asset in the UI, and record that choice explicitly under `user_scope` with
`origin=USER_SUPPLIED`. Both legacy and upgraded baselines receive this same
input even if the legacy implementation ignores a filter. A document mentioning
another asset does not acquire that asset as its primary scope.

An explicit option permits unscoped, same-family curated or official references
alongside the selected asset's documents. Case 033 has the exact initial-report
cutoff `2026-04-11T18:00:00+08:00`; future followup evidence must not appear merely
because it later explains the earlier event.

Answerability is independent from relevance and runtime outcome:

| Annotation | Meaning |
| --- | --- |
| `SUPPORTED` | Sources support the requested response, including a well-supported negative conclusion. |
| `INSUFFICIENT` | Relevant sources explain missing evidence but cannot establish the requested fact. These cases still have positive retrieval targets. |
| `AMBIGUOUS` | Identity remains ambiguous; the response must preserve the distinct candidates. |
| `DENIED` | The requested protected rows or markers are not available to this principal. Permitted summaries and public context may still be returned. |

These labels do not demonstrate that the running engine refused, answered
correctly, or applied permissions. Those outcomes require separate observation.

## Authorization pairs

Each protected request has an identically worded authorized control in the
same split. Engineering readers may receive published summaries while the
requested raw maintenance excerpt remains unavailable. Public readers may
receive permitted family references. An always-empty implementation therefore
cannot pass the paired evaluation.

The foreign-tenant case uses `tenant-alpha` with `public`, whose legacy corpus
must have a valid active index during integration. A missing index or backend
failure is a dependency result, not successful tenant isolation. A separate
paired-population integration check must also verify that changing hidden
industrial material does not change unauthorized visible results.

Annotated forbidden sections are sentinels, not the full policy. Evaluate the
ACL of every returned chunk, citation, provenance record and graph path,
including sources absent from the annotations. Do not use a blanket substring
ban: `SN-T09` and its registration mapping already appear in engineering-visible
records, and logical filenames can appear in published handovers or the user's
question. The protected content is the restricted source excerpt or private
marker, not every token that happens to occur in it.

## Separate PDF integration cases

`pdf-integration.json` pins three additional tests to original and normalized
artifact checksums and exact fragment ranges:

- Canalis KT physical pages 71–72: the test row, connection-state context and
  continuation footnote must remain together.
- HVX physical page 104: the MX1 candidate row spans two chunks and depends on
  its merged symptom header.
- HVX physical page 104: an unexpected-trip header is split across chunks,
  and several possible-cause rows require that header and the table columns.

These cases are excluded from authored-corpus primary metrics and require the
external cache at execution time. The offline validator verifies source catalog
edition pins and annotation structure; it does not claim to have executed PDF
retrieval. Integration must additionally verify the originals, normalized
artifact checksum and each text slice. Missing cache is explicitly reported;
it must not be presented as a passed PDF test. No manual originals or separate
photo/CSV files are bundled with these annotations.

## Repeatable checks

```sh
uv run python scripts/validate_industrial_gold.py
uv run python -m unittest tests.unit.test_industrial_gold -v
```

These checks require neither provider calls nor a database. They reject stale
corpus pins, shifted ranges, mismatched source hashes, unauthorized positives,
oracle identity filters, invalid time cutoffs, missing cases or pairs, redundant
complete-set supersets, altered semantic checksums, and invalid scalar types.
They establish annotation integrity; runtime retrieval, security and answer
quality remain separate measurements.
