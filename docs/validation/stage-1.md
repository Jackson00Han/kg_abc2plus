# Stage 1 Validation Record

Date: 2026-08-31  
Contract: `contracts/acceptance.v1.json` version 1.0.1

## Deliverables

- Human-readable scope, confirmed requirements, explicit assumptions, question
  classes, security boundary, and exclusions in `docs/acceptance_contract.md`.
- Testable terminology in `docs/glossary.md`.
- Machine-readable datasets, case quotas, metrics, thresholds, methods, and
  owners in `contracts/acceptance.v1.json`.
- Standard-library validator and unit tests.

## Reproducible Checks

```bash
python3 scripts/validate_acceptance_contract.py
python3 -m unittest tests.unit.test_acceptance_contract
python3 -m json.tool contracts/acceptance.v1.json >/dev/null
git diff --check
```

Expected result: the contract validator reports seven question classes and 20
measurable targets; all five unit tests pass; JSON and diff checks succeed.

## Decision

Stage 1 passes its exit criteria. The thresholds are explicit validation
assumptions because no deployment-specific corpus, traffic profile, or business
reviewers were supplied. A real deployment must version and reapprove changed
targets rather than silently reinterpret this contract.

Post-review clarification 1.0.1 adds the assumed update frequency, document
size, idempotency comparison boundary, and minimum answer-latency sample size.

## Local Development Profile Addendum

Date: 2026-09-01
Profile version: 1.0.0

The production contract remains unchanged at version 1.0.1. Two machine-checked
execution profiles now declare and resolve its workloads:

- `dev-mini` is the local default: 100 Chunks, two retrieval clients, 14 gold
  cases, 14 graph-review records, 100 load records, five answer requests, and a
  30-second load smoke run. Its Neo4j container is capped at 1.5 GiB and one CPU.
- `production-reference` applies no contract overrides and retains the original
  10,000-Chunk, eight-client, 49/50/10,000 dataset quotas, 30-request answer
  measurement, and five-minute sustained load.

All 20 metric definitions and targets are inherited unchanged. The reduced
profile is explicitly ineligible for `validation_complete` or production-
candidate evidence; quality is smoke-only and performance is informational.

At this milestone the profile CLI validates and resolves these declarations;
it does not generate the future evaluation corpus or execute load traffic. The
Stage 2/3 Neo4j runners enforce the matching local resource cap, while Stage
8/9 will make evaluation code consume the remaining corpus, concurrency,
sample-count, and duration fields.

Reproducible profile checks:

```bash
python3 scripts/validate_acceptance_contract.py
python3 scripts/validate_acceptance_contract.py --profile production-reference
python3 -m unittest tests.unit.test_acceptance_contract -v
```

Expected result: both profiles validate, `dev-mini` resolves to 100 Chunks,
`production-reference` resolves to 10,000 Chunks, and all 21 contract/profile
unit tests pass.
