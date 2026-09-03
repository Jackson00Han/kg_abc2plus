#!/bin/sh
set -eu

script_dir=$(CDPATH= cd "$(dirname "$0")" && pwd)
repo_root=$(dirname "$script_dir")
cd "$repo_root"

image="neo4j:5.26.12-community"
bolt_port="${PLAYGROUND_BOLT_PORT:-17692}"
container_memory="1536m"
container_cpus="1"
heap_initial="256m"
heap_max="512m"
pagecache="128m"
readiness_attempts="90"

case "$bolt_port" in
  ''|*[!0-9]*)
    echo "PLAYGROUND_BOLT_PORT must be an integer" >&2
    exit 2
    ;;
esac
if [ "$bolt_port" -lt 1024 ] || [ "$bolt_port" -gt 65535 ]; then
  echo "PLAYGROUND_BOLT_PORT must be between 1024 and 65535" >&2
  exit 2
fi

command -v docker >/dev/null 2>&1 || {
  echo "Docker is required for the local Playground" >&2
  exit 1
}
command -v python3 >/dev/null 2>&1 || {
  echo "Python 3 is required for the local Playground" >&2
  exit 1
}
command -v uv >/dev/null 2>&1 || {
  echo "uv is required for the local Playground" >&2
  exit 1
}
docker info >/dev/null 2>&1 || {
  echo "Docker is not running" >&2
  exit 1
}

run_suffix=$(python3 -c 'import secrets; print(secrets.token_hex(4))')
container_name="sample-graphrag-playground-${run_suffix}"
password=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')

cleanup() {
  docker stop "$container_name" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

echo "Starting disposable Neo4j for the local Playground"
docker run --rm --detach \
  --name "$container_name" \
  --label "com.sample-graphrag.purpose=local-playground" \
  --memory "$container_memory" \
  --memory-swap "$container_memory" \
  --cpus "$container_cpus" \
  --publish "127.0.0.1:${bolt_port}:7687" \
  --env "NEO4J_AUTH=neo4j/${password}" \
  --env "NEO4J_server_memory_heap_initial__size=${heap_initial}" \
  --env "NEO4J_server_memory_heap_max__size=${heap_max}" \
  --env "NEO4J_server_memory_pagecache_size=${pagecache}" \
  "$image" >/dev/null

attempt=0
until docker exec "$container_name" cypher-shell \
  -u neo4j -p "$password" "RETURN 1" >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge "$readiness_attempts" ]; then
    echo "Playground Neo4j did not become ready" >&2
    docker logs "$container_name" >&2
    exit 1
  fi
  sleep 1
done

env PLAYGROUND_NEO4J_URI="bolt://127.0.0.1:${bolt_port}" \
  PLAYGROUND_NEO4J_USER="neo4j" \
  PLAYGROUND_NEO4J_PASSWORD="$password" \
  PLAYGROUND_NEO4J_DATABASE="neo4j" \
  PLAYGROUND_ALLOW_DISPOSABLE_DB="1" \
  uv run --locked python -m scripts.run_playground "$@"
