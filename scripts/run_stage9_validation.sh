#!/bin/sh
set -eu
umask 077

output_dir=""
config_path="evaluation/production-reference-config.v1.json"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --output-dir)
      if [ "$#" -lt 2 ]; then
        echo "--output-dir requires a value" >&2
        exit 2
      fi
      output_dir=$2
      shift 2
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [ -z "$output_dir" ]; then
  echo "--output-dir is required" >&2
  exit 2
fi
case "$output_dir" in
  /*) ;;
  *)
    echo "--output-dir must be an absolute path outside the repository" >&2
    exit 2
    ;;
esac
case "$output_dir" in
  /|*/.|*/..|*".."*)
    echo "unsafe --output-dir: $output_dir" >&2
    exit 2
    ;;
esac
if [ -e "$output_dir" ]; then
  echo "output directory already exists: $output_dir" >&2
  exit 2
fi

repo_root=$(git rev-parse --show-toplevel 2>/dev/null) || {
  echo "Stage 9 validation must run from a Git checkout" >&2
  exit 1
}
repo_root=$(cd "$repo_root" && pwd -P)
if ! command -v python3 >/dev/null 2>&1; then
  echo "required command is unavailable: python3" >&2
  exit 1
fi
# Resolve without importing project code or writing bytecode before the external
# evidence directory is available for the workflow-wide cache prefix.
output_dir=$(PYTHONDONTWRITEBYTECODE=1 python3 - "$output_dir" "$repo_root" <<'PY'
from pathlib import Path
import sys

candidate = Path(sys.argv[1]).resolve(strict=False)
repository = Path(sys.argv[2]).resolve(strict=True)
if candidate == repository or repository in candidate.parents:
    print(
        f"--output-dir must be outside the repository: {repository}",
        file=sys.stderr,
    )
    raise SystemExit(2)
print(candidate)
PY
) || exit $?
case "$output_dir" in
  "$repo_root"|"$repo_root"/*)
    echo "--output-dir must be outside the repository: $repo_root" >&2
    exit 2
    ;;
esac

output_parent=$(dirname "$output_dir")
output_name=$(basename "$output_dir")
mkdir -p "$output_parent"
output_parent=$(cd "$output_parent" && pwd -P)
output_dir="$output_parent/$output_name"
case "$output_dir" in
  "$repo_root"|"$repo_root"/*)
    echo "--output-dir must be outside the repository: $repo_root" >&2
    exit 2
    ;;
esac
if [ -e "$output_dir" ] || [ -L "$output_dir" ]; then
  echo "output directory already exists: $output_dir" >&2
  exit 2
fi

mkdir "$output_dir"
raw_dir="$output_dir/raw"
load_dir="$raw_dir/load"
runtime_dir="$raw_dir/runtime"
backup_dir="$raw_dir/backup"
mkdir -p "$raw_dir" "$backup_dir" "$output_dir/reproduction"
PYTHONPYCACHEPREFIX="$output_dir/pycache"
export PYTHONPYCACHEPREFIX

cd "$repo_root"

for command_name in cmp diff docker git python3 sh uv; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "required command is unavailable: $command_name" >&2
    exit 1
  fi
done

code_commit=$(git rev-parse HEAD)
python3 - "$code_commit" <<'PY'
import re
import sys

if re.fullmatch(r"[0-9a-f]{40}", sys.argv[1]) is None:
    raise SystemExit("HEAD is not a full lowercase Git object ID")
PY

checkout_status=$(git status --porcelain --untracked-files=all) || {
  echo "could not inspect the Stage 9 checkout" >&2
  exit 1
}
if [ -n "$checkout_status" ]; then
  echo "Stage 9 requires a fully clean committed checkout" >&2
  git status --short --untracked-files=all >&2
  exit 1
fi

required_paths="
AGENTS.md
contracts/acceptance.v1.json
contracts/profiles/production-reference.v1.json
datasets/load-v1/NOTICE.txt
datasets/load-v1/manifest.json
evaluation/baselines/dev-mini.v1.json
evaluation/gold-v1/manifest.json
evaluation/production-reference-config.v1.json
evaluation/reference-answer-predictions.v1.json
scripts/build_backup_observation.py
scripts/build_load_corpus.py
scripts/build_production_report.py
scripts/load_production_corpus.py
scripts/run_large_database_quality.py
scripts/run_production_load.py
scripts/run_stage8_validation.sh
scripts/run_stage9_validation.sh
src/graphrag_prod/evaluation/production_config.py
"
for required_path in $required_paths; do
  if [ ! -f "$required_path" ]; then
    echo "required Stage 9 input is missing: $required_path" >&2
    exit 1
  fi
  if ! git ls-files --error-unmatch "$required_path" >/dev/null 2>&1; then
    echo "required Stage 9 input is not committed: $required_path" >&2
    exit 1
  fi
done

uv run --locked python - "$config_path" <<'PY'
import json
from pathlib import Path
import sys

from graphrag_prod.evaluation.production_config import (
    resolve_production_answer_retrieval_limits,
)

configuration = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
resolve_production_answer_retrieval_limits(configuration)
PY

json_value() {
  python3 - "$config_path" "$1" <<'PY'
import json
import sys

path, dotted = sys.argv[1:]
value = json.loads(open(path, encoding="utf-8").read())
for component in dotted.split("."):
    value = value[component]
if isinstance(value, bool):
    print("true" if value else "false")
elif isinstance(value, (str, int, float)):
    print(value)
else:
    raise SystemExit(f"configuration value is not scalar: {dotted}")
PY
}

validate_docker_cpu_capacity() {
  required_cpu_count=$1
  observed_cpu_count=$2
  case "$observed_cpu_count" in
    ''|*[!0-9]*)
      echo "Docker daemon returned an invalid .NCPU value: $observed_cpu_count" >&2
      return 1
      ;;
  esac
  if [ "$observed_cpu_count" -lt "$required_cpu_count" ]; then
    echo "Docker daemon exposes $observed_cpu_count CPU(s); production-reference requires at least $required_cpu_count" >&2
    return 1
  fi
}

require_docker_cpu_capacity() {
  required_cpu_count=$1
  if ! docker_daemon_cpu_count=$(docker info --format '{{.NCPU}}'); then
    echo "could not inspect Docker daemon CPU capacity" >&2
    return 1
  fi
  validate_docker_cpu_capacity \
    "$required_cpu_count" "$docker_daemon_cpu_count" || return $?
  echo "Docker daemon CPU capacity: $docker_daemon_cpu_count (required: $required_cpu_count)"
}

neo4j_image=$(json_value neo4j.image)
neo4j_digest=$(json_value neo4j.image_digest)
neo4j_database=$(json_value neo4j.database)
readiness_timeout=$(json_value neo4j.readiness_timeout_seconds)
transaction_timeout=$(json_value neo4j.transaction_timeout_seconds)
initial_load_transaction_timeout=$(
  json_value neo4j.initial_load_transaction_timeout_seconds
)
online_transaction_timeout=$(json_value neo4j.online_transaction_timeout_seconds)
container_cpus=$(json_value container.cpus)
container_memory_mb=$(json_value container.memory_mb)
heap_initial_mb=$(json_value container.heap_initial_mb)
heap_max_mb=$(json_value container.heap_max_mb)
pagecache_mb=$(json_value container.pagecache_mb)
configured_concurrency=$(json_value retrieval.concurrency)
configured_sustained_seconds=$(json_value retrieval.sustained_seconds)
configured_warmup_requests=$(json_value retrieval.warmup_requests)
configured_answer_samples=$(json_value answer.latency_samples)
configured_answer_warmup_requests=$(json_value answer.warmup_requests)
configured_minimum_chunks=$(json_value workload.minimum_chunks)

python3 - "$neo4j_digest" <<'PY'
import re
import sys

if re.fullmatch(r"sha256:[0-9a-f]{64}", sys.argv[1]) is None:
    raise SystemExit(
        "configured Neo4j image digest is not sha256:<64 lowercase hex>"
    )
PY
if [ "$neo4j_database" != "neo4j" ]; then
  echo "the production-reference workflow requires the neo4j database" >&2
  exit 1
fi
if [ "$readiness_timeout" -lt 1 ] || [ "$transaction_timeout" != "300" ] || \
   [ "$initial_load_transaction_timeout" != "60" ] || \
   [ "$online_transaction_timeout" != "5" ]; then
  echo "production-reference Neo4j timeout envelope drifted" >&2
  exit 1
fi
if [ "$container_cpus" != "8" ] || [ "$container_memory_mb" != "3072" ] || \
   [ "$heap_initial_mb" != "512" ] || [ "$heap_max_mb" != "1024" ] || \
   [ "$pagecache_mb" != "512" ]; then
  echo "production-reference Neo4j resource envelope drifted" >&2
  exit 1
fi
host_cpu_count=$(python3 -c 'import os; print(os.cpu_count() or 0)')
if [ "$host_cpu_count" -lt "$container_cpus" ]; then
  echo "host cannot supply the production-reference Neo4j CPU envelope" >&2
  exit 1
fi
if ! docker version >/dev/null; then
  echo "could not connect to the Docker daemon" >&2
  exit 1
fi
require_docker_cpu_capacity "$container_cpus"
if [ "$configured_concurrency" != "8" ] || \
   [ "$configured_sustained_seconds" -lt 300 ] || \
   [ "$configured_warmup_requests" != "64" ] || \
   [ "$configured_answer_samples" != "30" ] || \
   [ "$configured_answer_warmup_requests" != "30" ] || \
   [ "$configured_minimum_chunks" -lt 10000 ]; then
  echo "production-reference workload was weakened" >&2
  exit 1
fi

image_ref="$neo4j_image@$neo4j_digest"
password=$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(24))
PY
)
resource_suffix="$$"
source_container="sample-graphrag-stage9-source-$resource_suffix"
restore_container="sample-graphrag-stage9-restore-$resource_suffix"
dump_container="sample-graphrag-stage9-dump-$resource_suffix"
load_container="sample-graphrag-stage9-load-$resource_suffix"
source_volume="sample-graphrag-stage9-source-data-$resource_suffix"
restore_volume="sample-graphrag-stage9-restore-data-$resource_suffix"
ownership_label_key="com.sample-graphrag.stage9-run"
ownership_label_value=$(python3 - "$resource_suffix" <<'PY'
import secrets
import sys

print(f"{sys.argv[1]}-{secrets.token_hex(16)}")
PY
)

source_container_owned=0
restore_container_owned=0
dump_container_owned=0
load_container_owned=0
source_volume_owned=0
restore_volume_owned=0

# Flags make signal cleanup inspect only resources this process attempted to
# create; the unguessable label is the authoritative ownership check.
remove_owned_container() {
  owned_container=$1
  owned_flag=$2
  if [ "$owned_flag" != "1" ]; then
    return
  fi
  actual_owner=$(docker container inspect "$owned_container" \
    --format '{{ index .Config.Labels "com.sample-graphrag.stage9-run" }}' \
    2>/dev/null) || {
      echo "could not inspect owned Stage 9 container: $owned_container" >&2
      return 1
    }
  if [ "$actual_owner" != "$ownership_label_value" ]; then
    echo "refusing to remove container not owned by this run: $owned_container" >&2
    return 1
  fi
  if ! docker rm --force "$owned_container" >/dev/null 2>&1; then
    echo "failed to remove Stage 9 container: $owned_container" >&2
    return 1
  fi
}

verify_owned_container() {
  owned_container=$1
  actual_owner=$(docker container inspect "$owned_container" \
    --format '{{ index .Config.Labels "com.sample-graphrag.stage9-run" }}' \
    2>/dev/null) || return 1
  if [ "$actual_owner" != "$ownership_label_value" ]; then
    echo "container was not created by this Stage 9 run: $owned_container" >&2
    return 1
  fi
}

remove_owned_volume() {
  owned_volume=$1
  owned_flag=$2
  if [ "$owned_flag" != "1" ]; then
    return
  fi
  actual_owner=$(docker volume inspect "$owned_volume" \
    --format '{{ index .Labels "com.sample-graphrag.stage9-run" }}' \
    2>/dev/null) || {
      echo "could not inspect owned Stage 9 volume: $owned_volume" >&2
      return 1
    }
  if [ "$actual_owner" != "$ownership_label_value" ]; then
    echo "refusing to remove volume not owned by this run: $owned_volume" >&2
    return 1
  fi
  if ! docker volume rm --force "$owned_volume" >/dev/null 2>&1; then
    echo "failed to remove Stage 9 volume: $owned_volume" >&2
    return 1
  fi
}

verify_owned_volume() {
  owned_volume=$1
  actual_owner=$(docker volume inspect "$owned_volume" \
    --format '{{ index .Labels "com.sample-graphrag.stage9-run" }}' \
    2>/dev/null) || return 1
  if [ "$actual_owner" != "$ownership_label_value" ]; then
    echo "volume was not created by this Stage 9 run: $owned_volume" >&2
    return 1
  fi
}

cleanup_resources() {
  cleanup_failure=0
  remove_owned_container "$dump_container" "$dump_container_owned" || cleanup_failure=1
  remove_owned_container "$load_container" "$load_container_owned" || cleanup_failure=1
  remove_owned_container "$source_container" "$source_container_owned" || cleanup_failure=1
  remove_owned_container "$restore_container" "$restore_container_owned" || cleanup_failure=1
  remove_owned_volume "$source_volume" "$source_volume_owned" || cleanup_failure=1
  remove_owned_volume "$restore_volume" "$restore_volume_owned" || cleanup_failure=1
  return "$cleanup_failure"
}

cleanup() {
  cleanup_status=$?
  trap - EXIT HUP INT TERM
  cleanup_failure=0
  cleanup_resources || cleanup_failure=1
  if [ "$cleanup_status" -eq 0 ] && [ "$cleanup_failure" -ne 0 ]; then
    cleanup_status=1
  fi
  exit "$cleanup_status"
}

for container_name in \
  "$source_container" "$restore_container" "$dump_container" "$load_container"; do
  if docker container inspect "$container_name" >/dev/null 2>&1; then
    echo "refusing to reuse existing container: $container_name" >&2
    exit 1
  fi
done

for volume_name in "$source_volume" "$restore_volume"; do
  if docker volume inspect "$volume_name" >/dev/null 2>&1; then
    echo "refusing to reuse existing volume: $volume_name" >&2
    exit 1
  fi
done

trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

monotonic_ns() {
  # Use the project interpreter for both samples.  The Apple-supplied Python
  # 3.9 can expose a process-relative mach clock on some hosts, which makes
  # values collected by two short-lived processes incomparable.
  uv run --locked python - <<'PY'
import time
print(time.monotonic_ns())
PY
}

wait_for_neo4j() {
  wait_container=$1
  wait_attempt=0
  until NEO4J_USERNAME=neo4j NEO4J_PASSWORD="$password" \
    docker exec --env NEO4J_USERNAME --env NEO4J_PASSWORD \
    "$wait_container" cypher-shell "RETURN 1" >/dev/null 2>&1; do
    wait_attempt=$((wait_attempt + 1))
    if [ "$wait_attempt" -ge "$readiness_timeout" ]; then
      echo "Neo4j did not become ready: $wait_container" >&2
      docker logs "$wait_container" >&2 || true
      return 1
    fi
    sleep 1
  done
  published_address=$(docker port "$wait_container" 7687/tcp | \
    awk '/^127[.]0[.]0[.]1:/ {print; exit}')
  stage9_bolt_port=${published_address##*:}
  case "$stage9_bolt_port" in
    ''|*[!0-9]*)
      echo "could not resolve loopback Bolt port for $wait_container" >&2
      return 1
      ;;
  esac
}

start_neo4j() {
  start_container=$1
  start_volume=$2
  case "$start_container" in
    "$source_container") source_container_owned=1 ;;
    "$restore_container") restore_container_owned=1 ;;
    *)
      echo "refusing to create unexpected Stage 9 container: $start_container" >&2
      return 1
      ;;
  esac
  NEO4J_AUTH="neo4j/$password" docker run --detach \
    --name "$start_container" \
    --label "$ownership_label_key=$ownership_label_value" \
    --memory "${container_memory_mb}m" \
    --memory-swap "${container_memory_mb}m" \
    --cpus "$container_cpus" \
    --publish "127.0.0.1::7687" \
    --volume "$start_volume:/data" \
    --env NEO4J_AUTH \
    --env "NEO4J_server_memory_heap_initial__size=${heap_initial_mb}m" \
    --env "NEO4J_server_memory_heap_max__size=${heap_max_mb}m" \
    --env "NEO4J_server_memory_pagecache_size=${pagecache_mb}m" \
    --env "NEO4J_db_transaction_timeout=${transaction_timeout}s" \
    "$image_ref" >/dev/null
  verify_owned_container "$start_container"
  wait_for_neo4j "$start_container"
}

run_with_neo4j() {
  (
    unset OPENAI_API_KEY OPENAI_BASE_URL
    unset NEO4J_URI NEO4J_USER NEO4J_PASSWORD NEO4J_DATABASE
    export TEST_NEO4J_URI="bolt://127.0.0.1:$stage9_bolt_port"
    export TEST_NEO4J_USER="neo4j"
    export TEST_NEO4J_PASSWORD="$password"
    export TEST_NEO4J_DATABASE="$neo4j_database"
    export GRAPHRAG_ALLOW_DISPOSABLE_DB="1"
    "$@"
  )
}

verify_container_envelope() {
  envelope_container=$1
  envelope_output=${2:-}
  expected_memory_bytes=$((container_memory_mb * 1024 * 1024))
  expected_nano_cpus=$((container_cpus * 1000000000))
  actual_memory_bytes=$(docker container inspect "$envelope_container" \
    --format '{{.HostConfig.Memory}}')
  actual_memory_swap_bytes=$(docker container inspect "$envelope_container" \
    --format '{{.HostConfig.MemorySwap}}')
  actual_nano_cpus=$(docker container inspect "$envelope_container" \
    --format '{{.HostConfig.NanoCpus}}')
  actual_image_id=$(docker container inspect "$envelope_container" \
    --format '{{.Image}}')
  actual_environment_json=$(docker container inspect "$envelope_container" \
    --format '{{json .Config.Env}}')
  if [ "$actual_memory_bytes" != "$expected_memory_bytes" ] || \
     [ "$actual_memory_swap_bytes" != "$expected_memory_bytes" ] || \
     [ "$actual_nano_cpus" != "$expected_nano_cpus" ] || \
     [ "$actual_image_id" != "$expected_image_id" ]; then
    echo "Neo4j container does not match the fixed resource/image envelope" >&2
    return 1
  fi
  python3 - \
    "$envelope_output" "$neo4j_image" "$neo4j_digest" \
    "$actual_image_id" "$expected_image_id" \
    "$actual_memory_bytes" "$actual_memory_swap_bytes" \
    "$actual_nano_cpus" "$actual_environment_json" \
    "${heap_initial_mb}m" "${heap_max_mb}m" "${pagecache_mb}m" \
    "${transaction_timeout}s" <<'PY'
import json
from pathlib import Path
import sys

(
    output,
    image,
    digest,
    actual_image_id,
    expected_image_id,
    memory_bytes,
    memory_swap_bytes,
    nano_cpus,
    environment_json,
    heap_initial,
    heap_max,
    pagecache,
    transaction_timeout,
) = sys.argv[1:]
environment = set(json.loads(environment_json))
expected_environment = {
    f"NEO4J_server_memory_heap_initial__size={heap_initial}",
    f"NEO4J_server_memory_heap_max__size={heap_max}",
    f"NEO4J_server_memory_pagecache_size={pagecache}",
    f"NEO4J_db_transaction_timeout={transaction_timeout}",
}
missing = sorted(expected_environment - environment)
if missing:
    raise SystemExit(f"Neo4j container setting envelope is incomplete: {missing}")
value = {
    "actual_image_id": actual_image_id,
    "actual_memory_bytes": int(memory_bytes),
    "actual_memory_swap_bytes": int(memory_swap_bytes),
    "actual_nano_cpus": int(nano_cpus),
    "configured_image": image,
    "configured_heap_initial": heap_initial,
    "configured_heap_max": heap_max,
    "configured_pagecache": pagecache,
    "configured_repo_digest": digest,
    "configured_transaction_timeout": transaction_timeout,
    "expected_image_id": expected_image_id,
    "passed": True,
    "schema_version": "production-container-resource-observation-v1",
}
if output:
    Path(output).write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
PY
}

echo "Checking committed inputs and deterministic builders"
python3 scripts/validate_acceptance_contract.py
uv lock --check
uv run --locked python scripts/build_evaluation_gold.py --check
uv run --locked python scripts/build_dev_corpus.py --check
uv run --locked python -m scripts.build_load_corpus --check
git diff --check
for shell_script in scripts/*.sh; do
  sh -n "$shell_script"
done

echo "Resolving the exact Neo4j image: $image_ref"
docker pull "$image_ref"
expected_image_id=$(docker image inspect "$image_ref" --format '{{.Id}}')
if ! docker image inspect "$image_ref" --format '{{range .RepoDigests}}{{println .}}{{end}}' | \
  awk -F@ -v expected="$neo4j_digest" '$2 == expected { found = 1 } END { exit !found }'; then
  echo "pulled Neo4j image does not expose the committed repository digest" >&2
  exit 1
fi

echo "Running the complete Stage 8 workflow twice"
STAGE8_NEO4J_IMAGE="$image_ref" ./scripts/run_stage8_validation.sh \
  --repeat 2 \
  --baseline evaluation/baselines/dev-mini.v1.json \
  --output-dir "$output_dir/stage8"

echo "Materializing and rechecking the production-reference corpus"
materialized_load_dir="$raw_dir/materialized-load-v1"
uv run --locked python -m scripts.build_load_corpus \
  --materialize "$materialized_load_dir"
uv run --locked python -m scripts.build_load_corpus \
  --check-materialized "$materialized_load_dir"

echo "Starting the clean production-reference Neo4j environment"
source_volume_owned=1
docker volume create \
  --label "$ownership_label_key=$ownership_label_value" \
  "$source_volume" >/dev/null
verify_owned_volume "$source_volume"
restore_volume_owned=1
docker volume create \
  --label "$ownership_label_key=$ownership_label_value" \
  "$restore_volume" >/dev/null
verify_owned_volume "$restore_volume"
start_neo4j "$source_container" "$source_volume"
verify_container_envelope \
  "$source_container" "$raw_dir/source-container-resources.json"

container_inspection="$raw_dir/container-inspection.json"
run_with_neo4j uv run --locked python -m scripts.load_production_corpus \
  --inspect \
  --output "$container_inspection" \
  --actual-image "$neo4j_image" \
  --actual-repo-digest "$neo4j_digest" \
  --code-commit "$code_commit"

echo "Loading and validating the production-reference corpus"
run_with_neo4j uv run --locked python -m scripts.load_production_corpus \
  --load \
  --output-dir "$load_dir" \
  --initial-load-transaction-timeout-seconds \
  "$initial_load_transaction_timeout"
run_with_neo4j uv run --locked python -m scripts.run_large_database_quality \
  --config "$config_path" \
  --output "$raw_dir/large-database-quality.json"

pre_validation_graph_state="$raw_dir/pre-validation-graph-state.json"
post_validation_graph_state="$raw_dir/post-validation-graph-state.json"
backup_source_graph_state="$raw_dir/backup-source-graph-state.json"
restored_graph_state="$raw_dir/restored-graph-state.json"
run_with_neo4j uv run --locked python -m scripts.load_production_corpus \
  --fingerprint --output "$pre_validation_graph_state"

echo "Running the eight-client production-reference load window"
run_with_neo4j uv run --locked python -m scripts.run_production_load \
  --config "$config_path" \
  --output-dir "$runtime_dir" \
  --port 0
run_with_neo4j uv run --locked python -m scripts.load_production_corpus \
  --fingerprint --output "$post_validation_graph_state"

echo "Running exact deletion and cross-tenant preservation checks"
run_with_neo4j uv run --locked python -m scripts.load_production_corpus \
  --delete-test --output-dir "$load_dir"
run_with_neo4j uv run --locked python -m scripts.load_production_corpus \
  --fingerprint --output "$backup_source_graph_state"

echo "Stopping the source database for a real neo4j-admin dump/load cycle"
docker stop --time 60 "$source_container" >/dev/null
backup_started_ns=$(monotonic_ns)
dump_command="neo4j-admin database dump $neo4j_database --to-path=/backups --overwrite-destination=true"
load_command="neo4j-admin database load $neo4j_database --from-path=/backups --overwrite-destination=true"
dump_container_owned=1
docker create \
  --name "$dump_container" \
  --label "$ownership_label_key=$ownership_label_value" \
  --memory "${container_memory_mb}m" \
  --memory-swap "${container_memory_mb}m" \
  --cpus "$container_cpus" \
  --volume "$source_volume:/data" \
  --volume "$backup_dir:/backups" \
  --entrypoint neo4j-admin \
  "$image_ref" database dump "$neo4j_database" \
  --to-path=/backups --overwrite-destination=true >/dev/null
verify_owned_container "$dump_container"
docker start --attach "$dump_container"
dump_exit_code=$(docker container inspect "$dump_container" \
  --format '{{.State.ExitCode}}')
if [ "$dump_exit_code" != "0" ]; then
  echo "neo4j-admin dump failed with status $dump_exit_code" >&2
  exit 1
fi
docker rm "$dump_container" >/dev/null
dump_container_owned=0

dump_file="$backup_dir/$neo4j_database.dump"
if [ ! -f "$dump_file" ] || [ ! -s "$dump_file" ]; then
  echo "neo4j-admin did not produce a non-empty dump: $dump_file" >&2
  exit 1
fi

load_container_owned=1
docker create \
  --name "$load_container" \
  --label "$ownership_label_key=$ownership_label_value" \
  --memory "${container_memory_mb}m" \
  --memory-swap "${container_memory_mb}m" \
  --cpus "$container_cpus" \
  --volume "$restore_volume:/data" \
  --volume "$backup_dir:/backups:ro" \
  --entrypoint neo4j-admin \
  "$image_ref" database load "$neo4j_database" \
  --from-path=/backups --overwrite-destination=true >/dev/null
verify_owned_container "$load_container"
docker start --attach "$load_container"
load_exit_code=$(docker container inspect "$load_container" \
  --format '{{.State.ExitCode}}')
if [ "$load_exit_code" != "0" ]; then
  echo "neo4j-admin load failed with status $load_exit_code" >&2
  exit 1
fi
docker rm "$load_container" >/dev/null
load_container_owned=0

echo "Starting the fresh restored database"
start_neo4j "$restore_container" "$restore_volume"
verify_container_envelope \
  "$restore_container" "$raw_dir/restored-container-resources.json"
run_with_neo4j uv run --locked python -m scripts.load_production_corpus \
  --fingerprint --output "$restored_graph_state"
backup_finished_ns=$(monotonic_ns)

backup_observation="$raw_dir/backup-observation.json"
uv run --locked python -m scripts.build_backup_observation \
  --source-state "$backup_source_graph_state" \
  --restored-state "$restored_graph_state" \
  --started-ns "$backup_started_ns" \
  --finished-ns "$backup_finished_ns" \
  --dump-file "$dump_file" \
  --database "$neo4j_database" \
  --dump-command "$dump_command" \
  --load-command "$load_command" \
  --source-container-resources "$raw_dir/source-container-resources.json" \
  --restored-container-resources "$raw_dir/restored-container-resources.json" \
  --output "$backup_observation"

echo "Running final packaging, static, formatting, and repository checks"
uv build --out-dir "$output_dir/dist"
uv run --locked python -m compileall -q src tests scripts
for shell_script in scripts/*.sh; do
  sh -n "$shell_script"
done
git diff --check
if git grep -l -I -E -- \
  '-----BEGIN (RSA|EC|OPENSSH|DSA) PRIVATE KEY-----|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{36,}|sk-[A-Za-z0-9_-]{20,}' \
  -- . >/dev/null; then
  echo "credential-like material detected in committed files" >&2
  exit 1
else
  secret_scan_status=$?
  if [ "$secret_scan_status" -ne 1 ]; then
    echo "credential scan failed with status $secret_scan_status" >&2
    exit 1
  fi
fi
tracked_paths=$(git ls-files) || {
  echo "could not enumerate committed paths" >&2
  exit 1
}
if printf '%s\n' "$tracked_paths" | \
  awk '{
         if (($0 ~ /(^|\/)\.env($|\.)/ &&
              $0 !~ /(^|\/)\.env\.example$/) ||
             $0 ~ /(^|\/)(__pycache__|\.venv|venv|ENV|\.pytest_cache|\.mypy_cache|\.ruff_cache|\.tox|\.nox|htmlcov|build|dist)(\/|$)/ ||
             $0 ~ /(^|\/)[^\/]+\.egg-info(\/|$)/ ||
             $0 ~ /(^|\/)\.coverage($|\.)/ ||
             $0 ~ /\.(pyc|pyo|db|sqlite|sqlite3|dump|whl)$/ ||
             $0 ~ /\.tar\.gz$/ ||
             $0 ~ /^datasets\/load-v1\/(documents|chunks|entities|mentions)\.jsonl$/) {
           found = 1
         }
       }
       END { exit !found }'; then
  echo "unintended generated or secret-bearing file is committed" >&2
  exit 1
else
  artifact_scan_status=$?
  if [ "$artifact_scan_status" -ne 1 ]; then
    echo "committed artifact scan failed with status $artifact_scan_status" >&2
    exit 1
  fi
fi
checkout_status=$(git status --porcelain --untracked-files=all) || {
  echo "could not reinspect the Stage 9 checkout" >&2
  exit 1
}
if [ -n "$checkout_status" ]; then
  echo "validation changed or generated files inside the repository" >&2
  git status --short --untracked-files=all >&2
  exit 1
fi

# A report with production_candidate_eligible=true is written only below this
# excluded working tree until every final check and byte-reproduction gate has
# passed. The authoritative report is moved into place as the last fallible
# publication step.
qualification_working="$output_dir/.qualification-working"
primary_working="$qualification_working/primary"
reproduction_working="$qualification_working/reproduction"
mkdir -p "$primary_working" "$reproduction_working"

echo "Building the evidence-bound production-candidate report twice"
uv run --locked python -m scripts.build_production_report \
  --stage8-report "$output_dir/stage8/run-1/report.json" \
  --stage8-suites-dir "$output_dir/stage8/run-1/suites" \
  --load-dir "$load_dir" \
  --runtime-dir "$runtime_dir" \
  --container-inspection "$container_inspection" \
  --container-resources "$raw_dir/source-container-resources.json" \
  --pre-graph-state "$pre_validation_graph_state" \
  --post-graph-state "$post_validation_graph_state" \
  --backup-source-graph-state "$backup_source_graph_state" \
  --restore-graph-state "$restored_graph_state" \
  --backup-observation "$backup_observation" \
  --backup-dump "$dump_file" \
  --large-database-quality "$raw_dir/large-database-quality.json" \
  --code-commit "$code_commit" \
  --observations-output "$primary_working/observations.json" \
  --output "$primary_working/report.json"

uv run --locked python -m scripts.build_production_report \
  --stage8-report "$output_dir/stage8/run-1/report.json" \
  --stage8-suites-dir "$output_dir/stage8/run-1/suites" \
  --load-dir "$load_dir" \
  --runtime-dir "$runtime_dir" \
  --container-inspection "$container_inspection" \
  --container-resources "$raw_dir/source-container-resources.json" \
  --pre-graph-state "$pre_validation_graph_state" \
  --post-graph-state "$post_validation_graph_state" \
  --backup-source-graph-state "$backup_source_graph_state" \
  --restore-graph-state "$restored_graph_state" \
  --backup-observation "$backup_observation" \
  --backup-dump "$dump_file" \
  --large-database-quality "$raw_dir/large-database-quality.json" \
  --code-commit "$code_commit" \
  --observations-output "$reproduction_working/observations.json" \
  --output "$reproduction_working/report.json"

cmp "$primary_working/report.json" "$reproduction_working/report.json"
cmp "$primary_working/observations.json" \
  "$reproduction_working/observations.json"
diff -r "$primary_working/evidence" "$reproduction_working/evidence"
python3 - "$primary_working/report.json" <<'PY'
import json
import sys

report = json.loads(open(sys.argv[1], encoding="utf-8").read())
if report.get("passed") is not True:
    raise SystemExit("Stage 9 report did not pass")
if report.get("production_candidate_eligible") is not True:
    raise SystemExit("Stage 9 report is not production-candidate eligible")
PY

# Report construction must also remain repository-read-only. Publish evidence
# only after the exact owned Docker resources have been removed, then publish
# evidence first, observations second, and the authoritative report last.
checkout_status=$(git status --porcelain --untracked-files=all) || {
  echo "could not reinspect the Stage 9 checkout" >&2
  exit 1
}
if [ -n "$checkout_status" ]; then
  echo "validation changed or generated files inside the repository" >&2
  git status --short --untracked-files=all >&2
  exit 1
fi
cleanup_resources
source_container_owned=0
restore_container_owned=0
dump_container_owned=0
load_container_owned=0
source_volume_owned=0
restore_volume_owned=0
mv "$primary_working/evidence" "$output_dir/evidence"
mv "$primary_working/observations.json" "$output_dir/observations.json"
mv "$reproduction_working/evidence" "$output_dir/reproduction/evidence"
mv "$reproduction_working/observations.json" \
  "$output_dir/reproduction/observations.json"
mv "$reproduction_working/report.json" "$output_dir/reproduction/report.json"
mv "$primary_working/report.json" "$output_dir/report.json"

echo "Stage 9 validation complete: $output_dir"
echo "Implementation commit: $code_commit"
