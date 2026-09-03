#!/bin/sh
set -eu

repeat=2
output_dir=""
baseline="evaluation/baselines/dev-mini.v1.json"
baseline_candidate=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --repeat)
      repeat=$2
      shift 2
      ;;
    --output-dir)
      output_dir=$2
      shift 2
      ;;
    --baseline)
      baseline=$2
      shift 2
      ;;
    --baseline-candidate)
      baseline_candidate=$2
      shift 2
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

case "$repeat" in
  ''|*[!0-9]*|0)
    echo "--repeat must be a positive integer" >&2
    exit 2
    ;;
esac

if [ -z "$output_dir" ]; then
  echo "--output-dir is required" >&2
  exit 2
fi
case "$output_dir" in
  /|.|..|*".."*)
    echo "unsafe --output-dir" >&2
    exit 2
    ;;
esac
if [ -e "$output_dir" ]; then
  echo "output directory already exists: $output_dir" >&2
  exit 2
fi
mkdir -p "$output_dir"

if [ -z "$baseline_candidate" ] && [ ! -f "$baseline" ]; then
  echo "reviewed baseline is missing: $baseline" >&2
  exit 2
fi
if [ -n "$baseline_candidate" ] && [ "$repeat" -ne 1 ]; then
  echo "baseline candidate capture requires --repeat 1" >&2
  exit 2
fi

iteration=1
while [ "$iteration" -le "$repeat" ]; do
  run_dir="$output_dir/run-$iteration"
  suites_dir="$run_dir/suites"
  observations_dir="$run_dir/observations"
  mkdir -p "$suites_dir" "$observations_dir"

  uv run --locked python scripts/run_test_suite.py \
    --start tests/unit --output "$suites_dir/unit.json" --require-no-skips
  uv run --locked python scripts/run_test_suite.py \
    --start tests/e2e --output "$suites_dir/e2e.json" --require-no-skips
  uv run --locked python scripts/run_test_suite.py \
    --start tests/security --output "$suites_dir/security.json" --require-no-skips
  uv run --locked python scripts/validate_security_suite.py \
    --results "$suites_dir/security.json"
  uv run --locked python scripts/run_test_suite.py \
    --start tests/regression --output "$suites_dir/regression.json" \
    --require-no-skips
  ./scripts/run_stage8_neo4j_tests.sh \
    "$suites_dir/integration.json" "$observations_dir"
  uv run --locked python scripts/capture_conflict_results.py \
    --output "$observations_dir/conflict-results.jsonl"
  uv run --locked python scripts/evaluate_knowledge_quality.py \
    --gold evaluation/knowledge-quality-v1/gold.json \
    --predictions evaluation/knowledge-quality-v1/predictions.json \
    --policy evaluation/knowledge-quality-v1/policy.json \
    --baseline evaluation/knowledge-quality-v1/baseline.json \
    --output "$run_dir/knowledge-quality-report.json"

  if [ -n "$baseline_candidate" ]; then
    uv run --locked python scripts/run_evaluation.py \
      --observations-dir "$observations_dir" \
      --suite-results-dir "$suites_dir" \
      --knowledge-quality-report "$run_dir/knowledge-quality-report.json" \
      --output "$run_dir/report.json" \
      --baseline-candidate "$baseline_candidate"
  else
    uv run --locked python scripts/run_evaluation.py \
      --observations-dir "$observations_dir" \
      --suite-results-dir "$suites_dir" \
      --knowledge-quality-report "$run_dir/knowledge-quality-report.json" \
      --output "$run_dir/report.json" \
      --baseline "$baseline"
  fi
  iteration=$((iteration + 1))
done

if [ "$repeat" -gt 1 ]; then
  first_report="$output_dir/run-1/report.json"
  iteration=2
  while [ "$iteration" -le "$repeat" ]; do
    uv run --locked python scripts/compare_evaluation_reports.py \
      "$first_report" "$output_dir/run-$iteration/report.json"
    iteration=$((iteration + 1))
  done
fi

uv run --locked python scripts/build_evaluation_gold.py --check
uv run --locked python scripts/build_dev_corpus.py --check
python3 scripts/validate_acceptance_contract.py
uv lock --check
uv build --out-dir "$output_dir/dist"
uv run --locked python -m compileall -q src tests scripts
for script in scripts/*.sh; do
  sh -n "$script"
done
git diff --check

echo "Stage 8 validation complete: $output_dir"
