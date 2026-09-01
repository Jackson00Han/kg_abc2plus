#!/bin/sh
set -eu

container_name="sample-graphrag-stage2-neo4j"
image="neo4j:5.26.12-community"
password="stage2-test-password"
bolt_port="17687"

if docker container inspect "$container_name" >/dev/null 2>&1; then
  echo "Refusing to reuse existing container: $container_name" >&2
  exit 1
fi

cleanup() {
  docker stop "$container_name" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

echo "Starting disposable Neo4j container: $container_name"
docker run --rm --detach \
  --name "$container_name" \
  --publish "127.0.0.1:${bolt_port}:7687" \
  --env "NEO4J_AUTH=neo4j/${password}" \
  "$image" >/dev/null

echo "Waiting for Neo4j on 127.0.0.1:${bolt_port}"
attempt=0
until docker exec "$container_name" cypher-shell \
  -u neo4j -p "$password" "RETURN 1" >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    echo "Neo4j test container did not become ready" >&2
    docker logs "$container_name" >&2
    exit 1
  fi
  sleep 1
done

echo "Running Stage 2 Neo4j integration tests"
env -u OPENAI_API_KEY -u OPENAI_BASE_URL \
  -u NEO4J_URI -u NEO4J_PASSWORD \
  TEST_NEO4J_URI="bolt://127.0.0.1:${bolt_port}" \
  TEST_NEO4J_USER="neo4j" \
  TEST_NEO4J_PASSWORD="$password" \
  TEST_NEO4J_DATABASE="neo4j" \
  GRAPHRAG_ALLOW_DISPOSABLE_DB="1" \
  uv run --locked python -m unittest discover \
  -s tests/integration -p 'test_*neo4j.py' -v
