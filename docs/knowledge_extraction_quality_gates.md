# Knowledge Extraction Quality and Drift Gates

This project evaluates ontology-constrained knowledge extraction with an
offline, deterministic gate implemented in
`graphrag_prod.evaluation.knowledge_quality`. The gate measures extraction and
entity-resolution behavior; it does not rank retrieval results and does not
introduce a retrieval scoring formula.

The trust boundary is deliberate:

> An LLM extraction remains secondary derived data even when it passes every
> quality gate. Passing evaluation never promotes it to the authoritative
> layer.

Authoritative entities, relationships, and property facts still require the
separate expert import/publication workflow. Industrial high-risk types require
a terminal human decision for every candidate. Low-risk types may use a
configured human-review sample selected by the surrounding review workflow;
the observed sample rate is measured and gated here.

## Versioned adjudicated gold

Gold uses schema `knowledge-extraction-gold-v1`. It is independent of system
predictions and has this top-level shape:

```json
{
  "schema_version": "knowledge-extraction-gold-v1",
  "dataset_id": "pump-maintenance-gold",
  "version": "1.0.0",
  "contains_predictions": false,
  "adjudication": {
    "status": "approved",
    "protocol_version": "industrial-review-v1",
    "approved_case_ids": ["positive-01", "negative-01", "security-01"]
  },
  "ontology": {
    "entity_types": ["Pump", "Line"],
    "relationship_types": [
      {
        "type": "FEEDS",
        "source_entity_types": ["Pump"],
        "target_entity_types": ["Line"]
      }
    ],
    "property_types": [
      {
        "type": "operating_pressure",
        "owner_entity_types": ["Pump"],
        "datatype": "DECIMAL",
        "canonical_unit": "bar"
      }
    ]
  },
  "cases": []
}
```

Every case binds one immutable `document_id`, `chunk_id`, and `chunk_text` and
is classified as `positive`, `negative`, or `security`. A dataset is rejected
unless it contains both negative and security cases. Predictions must include
exactly every gold case, including cases with empty outputs. This prevents a
caller from improving a denominator by omitting difficult, negative, or
security-sensitive examples.

The top-level adjudication record must be `approved`, name a versioned review
protocol, and list every case ID exactly once. Gold that is partially reviewed
or silently extended after adjudication is rejected.

A positive case may adjudicate four kinds of output:

- entity mentions: entity type, canonical name, mention text, and exact
  evidence;
- relationships: allowed relationship type and source/target mention IDs;
- typed property facts: owner mention, property type, all typed-literal fields,
  raw/canonical units, and raw/canonical validity/observation times;
- entity-resolution pairs: two mention IDs and the adjudicated `should_merge`
  decision. Gold must include at least one positive and one negative pair.

Every entity, relationship, and property fact contains an exact evidence
object:

```json
{
  "document_id": "document-uuid",
  "chunk_id": "chunk-uuid",
  "start": 37,
  "end": 43,
  "quote": "12 bar"
}
```

The loader proves `chunk_text[start:end] == quote` and verifies the Document and
Chunk IDs against the containing case. Entity mention text must occur inside
its own quote. Relationship evidence must contain both endpoint mentions.
Property evidence must contain the exact raw value, raw unit, and every raw
temporal qualifier. Gold with a schema, datatype, unit, temporal, endpoint, or
evidence error is invalid rather than becoming a system failure.

Property datatypes exactly match the production T-Box: `STRING`, `INTEGER`,
`FLOAT`, `DECIMAL`, `BOOLEAN`, `DATE`, `DATETIME`, `DURATION`, `URI`, and
`JSON`. `typed_value` retains the appropriate scalar, while `canonical_value`
is always a deterministic string. In particular, decimals remain strings and
are never routed through a binary float. `raw_unit` is the exact source token;
`canonical_unit` is the T-Box unit. All raw time qualifiers require RFC3339
with an explicit offset, canonical time qualifiers use UTC `Z`, and
`valid_from` must be strictly earlier than `valid_to`.

## Independent predictions and trust metadata

System observations use `knowledge-extraction-predictions-v1` and bind the
exact dataset ID and gold version. Each predicted artifact repeats the factual
fields and adds:

- `origin`: `llm`, `rule`, or `expert`;
- `authority_level`: `secondary` or `authoritative`;
- `review_status`: `pending`, `approved`, `rejected`, or `quarantined`.

An artifact with any non-expert origin (`llm` or `rule`) and
`authority_level: authoritative` is counted as authority contamination. The
recommended and test-fixture threshold is zero. Human approval of an LLM
candidate does not rewrite its extraction origin, and the evaluation gate
itself never changes authority.

## Metrics

The report schema is `knowledge-quality-report-v1`. It contains deterministic
counters and rates for:

- overall and per-family entity, relationship, and property precision, recall,
  and F1;
- per-type precision, recall, and F1 (`entity:Pump`,
  `relationship:FEEDS`, and `property:operating_pressure`);
- ontology/schema violations and exact-evidence violations;
- non-expert-to-authoritative contamination;
- human approve, reject, quarantine, and pending rates;
- high-risk pending count and low-risk review sample rate;
- resolution false-merge rate over adjudicated negative pairs and missed-merge
  rate over adjudicated positive pairs;
- false positives split by positive, negative, and security case class.

Exact semantic and evidence keys are matched as multisets. Duplicate
predictions therefore remain false positives instead of disappearing through
set deduplication. A security case that produces any artifact is always a hard
failure, independently of aggregate precision.

## Bounded policy and drift

Policy schema `knowledge-quality-gate-policy-v1` configures absolute thresholds,
per-type F1 minimums, high-risk types, low-risk sampling, drift tolerances, and
resource bounds. The implementation also enforces non-configurable ceilings:

- no more than 10,000 cases;
- no more than 1,000 artifacts of each family per case;
- no more than 1,000 resolution pairs per case;
- no more than 50,000,000 total gold text characters;
- no more than 32 MiB per CLI input file.

Configured limits cannot exceed those ceilings. Thresholds and drift
tolerances must be finite ratios in `[0, 1]`. High-risk and per-type policy keys
must name types declared by the gold ontology.

A normal run requires an explicitly locked
`knowledge-quality-baseline-v1`. The baseline binds the exact dataset ID, gold
version, canonical gold SHA-256 digest, policy version, and canonical policy
digest. It stores overall, family, and per-type F1 plus violation, resolution,
reject, and quarantine rates. The gate reports drops/increases and fails when
any configured tolerance is exceeded.

`knowledge_baseline_candidate()` intentionally emits `"locked": false`.
Generating a candidate is not approval: a maintainer must inspect the gold,
case results, policy, and metric changes before explicitly locking and
versioning it. A missing, unlocked, malformed, or stale baseline fails closed.

## Committed Stage 8 reference assets

The repository includes one executable, provider-free reference gate under
`evaluation/knowledge-quality-v1/`:

- `gold.json`: independently adjudicated `1.0.0` gold with one positive, one
  negative, and one restricted-content security case;
- `predictions.json`: separately versioned reference extraction observations;
- `policy.json`: bounded absolute thresholds, high-risk review requirements,
  and zero-tolerance drift limits;
- `baseline.json`: the explicitly reviewed and locked baseline bound to the
  exact canonical gold and policy digests.

The positive case covers repeated and distinct same-type entities, a typed
relationship, exact evidence spans, a `1200 kPa` to `12 bar` decimal property,
UTC-normalized validity/observation times, and both positive and negative
entity-resolution pairs. The negative and security cases require empty output.
All model-origin reference artifacts remain `secondary`, even though their
review state is `approved`.

These predictions are a deterministic offline reference artifact, not a
provider call and not part of the gold object. Replacing them with observations
from a new extractor requires a new `extractor_version` and a separately
reviewed prediction artifact. Any metric, type-inventory, or configured drift
change then requires the normal baseline-candidate review; a generated
candidate is never locked automatically.

Every iteration of `scripts/run_stage8_validation.sh` invokes this gate under
shell `set -e` and writes
`<stage8-output>/run-N/knowledge-quality-report.json`. Missing, malformed,
unlocked, stale, or below-threshold assets therefore stop Stage 8. The unified
Stage 8 report independently recomputes that report from all four assets and
binds their file hashes, prediction/extractor identity, metrics, and report
digest into its deterministic projection. The normal two-run comparison and
semantic baseline therefore cover this gate as well as its standalone exit
status.

## Offline command

Run the committed gate without Neo4j or provider access:

```bash
uv run --locked python scripts/evaluate_knowledge_quality.py \
  --gold evaluation/knowledge-quality-v1/gold.json \
  --predictions evaluation/knowledge-quality-v1/predictions.json \
  --policy evaluation/knowledge-quality-v1/policy.json \
  --baseline evaluation/knowledge-quality-v1/baseline.json \
  --output /tmp/sample-graphrag-knowledge-quality-report.json
```

The command exits `0` only when the validated report passes every absolute and
drift gate. A valid but failing run still writes its report and exits `1`.
Malformed or unbounded input is rejected before metric construction and exits
`1`.

To record a candidate alongside a normal run, add:

```bash
  --baseline-candidate /tmp/knowledge-quality-baseline-candidate.json
```

The report excludes clocks, random values, machine identity, and provider
calls. Its `report_digest` is SHA-256 over canonical JSON before the digest
field is added, so the same versioned inputs produce the same report and digest
on repeated runs.

## Industrial operating policy

For safety-, compliance-, quality-, maintenance-, and process-critical entity,
relationship, or property types, configure the type as high risk and require
an expert to approve, reject, or quarantine every candidate before publication.
Quarantined and rejected candidates remain evaluation observations but must not
enter the active graph publication.

For genuinely low-risk navigation data, a sampled review policy can reduce
manual workload. Sampling does not raise authority: accepted automatic facts
remain secondary, retain exact source evidence and extractor version, and can
be filtered separately from expert-authored A-Box facts during retrieval.
