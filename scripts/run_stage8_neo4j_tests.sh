#!/bin/sh
set -eu

if [ "$#" -ne 2 ]; then
  echo "usage: $0 SUITE_RESULT_PATH OBSERVATION_DIR" >&2
  exit 2
fi

suite_result_path=$1
observation_dir=$2
container_name="sample-graphrag-stage8-neo4j-$$"
image=${STAGE8_NEO4J_IMAGE:-neo4j:5.26.12-community}
password="stage8-test-password"
container_memory="1536m"
container_cpus="1"
heap_initial="256m"
heap_max="512m"
pagecache="128m"
readiness_attempts="90"
ownership_label_key="com.sample-graphrag.stage8-run"
ownership_label_value=$(python3 - <<'PY'
import secrets

print(secrets.token_hex(16))
PY
)
container_owned=0

case "$suite_result_path:$observation_dir" in
  *".."*)
    echo "Stage 8 output paths must not contain parent traversal" >&2
    exit 2
    ;;
esac

if docker container inspect "$container_name" >/dev/null 2>&1; then
  echo "Refusing to reuse existing container: $container_name" >&2
  exit 1
fi

cleanup() {
  cleanup_status=$?
  trap - EXIT HUP INT TERM
  if [ "$container_owned" = "1" ]; then
    actual_owner=$(docker container inspect "$container_name" \
      --format '{{ index .Config.Labels "com.sample-graphrag.stage8-run" }}' \
      2>/dev/null) || actual_owner=""
    if [ "$actual_owner" = "$ownership_label_value" ]; then
      if ! docker rm --force "$container_name" >/dev/null 2>&1; then
        echo "Failed to remove owned Stage 8 container: $container_name" >&2
        if [ "$cleanup_status" -eq 0 ]; then
          cleanup_status=1
        fi
      fi
    elif [ -n "$actual_owner" ]; then
      echo "Refusing to remove a Stage 8 container owned by another run" >&2
      if [ "$cleanup_status" -eq 0 ]; then
        cleanup_status=1
      fi
    elif [ "$cleanup_status" -eq 0 ]; then
      echo "Could not inspect the owned Stage 8 container: $container_name" >&2
      cleanup_status=1
    fi
  fi
  exit "$cleanup_status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir -p "$(dirname "$suite_result_path")" "$observation_dir"

echo "Starting disposable Stage 8 Neo4j container: $container_name"
container_owned=1
docker run --rm --detach \
  --name "$container_name" \
  --label "$ownership_label_key=$ownership_label_value" \
  --memory "$container_memory" \
  --memory-swap "$container_memory" \
  --cpus "$container_cpus" \
  --publish "127.0.0.1::7687" \
  --env "NEO4J_AUTH=neo4j/${password}" \
  --env "NEO4J_server_memory_heap_initial__size=${heap_initial}" \
  --env "NEO4J_server_memory_heap_max__size=${heap_max}" \
  --env "NEO4J_server_memory_pagecache_size=${pagecache}" \
  "$image" >/dev/null
actual_owner=$(docker container inspect "$container_name" \
  --format '{{ index .Config.Labels "com.sample-graphrag.stage8-run" }}')
if [ "$actual_owner" != "$ownership_label_value" ]; then
  echo "Stage 8 container ownership verification failed" >&2
  exit 1
fi
published_address=$(docker port "$container_name" 7687/tcp | \
  awk '/^127[.]0[.]0[.]1:/ {print; exit}')
bolt_port=${published_address##*:}
case "$bolt_port" in
  ''|*[!0-9]*)
    echo "Could not resolve the Stage 8 loopback Bolt port" >&2
    exit 1
    ;;
esac

echo "Waiting for Neo4j on 127.0.0.1:${bolt_port}"
attempt=0
until docker exec "$container_name" cypher-shell \
  -u neo4j -p "$password" "RETURN 1" >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge "$readiness_attempts" ]; then
    echo "Neo4j test container did not become ready" >&2
    docker logs "$container_name" >&2
    exit 1
  fi
  sleep 1
done

echo "Running Stage 8 Neo4j integration tests"
env -u OPENAI_API_KEY -u OPENAI_BASE_URL \
  -u NEO4J_URI -u NEO4J_PASSWORD \
  TEST_NEO4J_URI="bolt://127.0.0.1:${bolt_port}" \
  TEST_NEO4J_USER="neo4j" \
  TEST_NEO4J_PASSWORD="$password" \
  TEST_NEO4J_DATABASE="neo4j" \
  GRAPHRAG_ALLOW_DISPOSABLE_DB="1" \
  GRAPHRAG_EVALUATION_OUTPUT_DIR="$observation_dir" \
  uv run --locked python scripts/run_test_suite.py \
  --start tests/integration \
  --pattern 'test_*neo4j.py' \
  --output "$suite_result_path" \
  --require-no-skips
