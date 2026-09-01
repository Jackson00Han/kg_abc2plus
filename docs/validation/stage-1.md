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
