#!/bin/sh
set -eu

if [ "$#" -ne 2 ]; then
  echo "usage: $0 SUITE_RESULT_PATH OBSERVATION_DIR" >&2
  exit 2
fi

suite_result_path=$1
observation_dir=$2
container_name="sample-graphrag-stage8-neo4j-$$"
image="neo4j:5.26.12-community"
password="stage8-test-password"
bolt_port=$((17700 + ($$ % 500)))
container_memory="1536m"
container_cpus="1"
heap_initial="256m"
heap_max="512m"
pagecache="128m"
readiness_attempts="90"

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
  docker stop "$container_name" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

mkdir -p "$(dirname "$suite_result_path")" "$observation_dir"

echo "Starting disposable Stage 8 Neo4j container: $container_name"
docker run --rm --detach \
  --name "$container_name" \
  --memory "$container_memory" \
  --memory-swap "$container_memory" \
  --cpus "$container_cpus" \
  --publish "127.0.0.1:${bolt_port}:7687" \
  --env "NEO4J_AUTH=neo4j/${password}" \
  --env "NEO4J_server_memory_heap_initial__size=${heap_initial}" \
  --env "NEO4J_server_memory_heap_max__size=${heap_max}" \
  --env "NEO4J_server_memory_pagecache_size=${pagecache}" \
  "$image" >/dev/null

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
