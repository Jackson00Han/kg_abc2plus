#!/usr/bin/env python3
"""Initialize a new, empty workbench without importing data or calling models."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import sys
import time
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from neo4j import GraphDatabase
from neo4j.exceptions import Neo4jError, ServiceUnavailable

from restart_workbench import private_environment
from run_playground import _loopback_neo4j_uri, _official_provider_base_url
from graphrag_prod.domain.ids import embedding_space_id
from graphrag_prod.graph.schema import apply_schema, verify_schema
from graphrag_prod.ingestion import Neo4jEmbeddingIndexManager
from graphrag_prod.playground.workspaces import (
    CreateWorkspace,
    Neo4jWorkspaceStore,
    _fingerprint,
    _project_id,
    prepare_workspace_tenant,
)

FIRST_OPERATION = str(uuid5(NAMESPACE_URL, "kg-abc2plus:first-workspace:v1"))
ACTOR = "workbench-initializer"


def embedding_profile(environment: dict[str, str]) -> SimpleNamespace:
    # Use the same provider identity and dimensions as the serving entry point.
    # This metadata object has no model client and cannot issue API requests.
    _official_provider_base_url(environment["OPENAI_BASE_URL"])
    try:
        dimensions = int(environment["EMBEDDING_DIMENSIONS"])
    except ValueError as error:
        raise ValueError("EMBEDDING_DIMENSIONS must be an integer") from error
    if dimensions not in {64, 128, 256, 512, 768, 1024, 1536, 2048}:
        raise ValueError("EMBEDDING_DIMENSIONS is not supported by text-embedding-v4")
    profile = dict(provider="dashscope-openai-compatible",
                   model=environment["EMBEDDING_MODEL"], revision="api-v1",
                   dimensions=dimensions, normalization="provider-default")
    return SimpleNamespace(**profile, embedding_space_id=embedding_space_id(**profile))


def require_schema(driver, database: str) -> None:
    errors = verify_schema(driver, database)
    if errors:
        raise RuntimeError("Database schema is not ready: " + "; ".join(errors[:5]))


def prepare_empty_or_reuse(driver, database: str, store, project: dict, profile) -> None:
    tenant = project["active_tenant_id"]
    active = Neo4jEmbeddingIndexManager(driver, database).active_generation(tenant)
    if active is None and any(store.counts(tenant).values()):
        raise RuntimeError("Existing knowledge has no active vector index; use the normal index recovery workflow")
    # Existing compatible indexes are read only. An interrupted first setup can
    # prepare the empty index after its workspace transaction was committed.
    prepare_workspace_tenant(driver, database, profile, tenant)


def initialize(driver, database: str, name: str, profile) -> dict:
    request = CreateWorkspace(name=name, operation_id=FIRST_OPERATION)
    store = Neo4jWorkspaceStore(driver, database, pump_only=True)
    projects = store.list()
    if projects:
        require_schema(driver, database)
        for project in projects:
            prepare_empty_or_reuse(driver, database, store, project, profile)
        return {"status": "existing", "workspaces": len(projects)}

    records, _, _ = driver.execute_query(
        "MATCH (n) RETURN 1 AS present LIMIT 1", database_=database)
    if records:
        raise RuntimeError("Database contains data but no visible workbench workspace; refusing initialization")

    apply_schema(driver, database)
    require_schema(driver, database)
    project_id = _project_id(request.operation_id)
    now = datetime.now(UTC).isoformat()
    project = dict(knowledge_base_id=project_id, name=request.name,
                   name_key=request.name.casefold(),
                   active_tenant_id="workspace-" + project_id, generation=1,
                   admin_groups=["members", "administrators"], user_groups=["members"],
                   created_at=now, updated_at=now)
    # Commit the workspace first, so a retry can recover an interrupted empty
    # vector-index preparation without treating its metadata as unrelated data.
    project = store.save(project, previous=None, operation_id=request.operation_id,
                         fingerprint=_fingerprint("create", ACTOR, request.model_dump()), actor=ACTOR)
    prepare_empty_or_reuse(driver, database, store, project, profile)
    return {"status": "created", "workspaces": 1,
            "knowledge_base_id": project["knowledge_base_id"], "name": project["name"],
            "business_data_counts": store.counts(project["active_tenant_id"])}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="我的知识库", help="name for the first empty knowledge base")
    args = parser.parse_args()
    try:
        environment = private_environment()
        profile = embedding_profile(environment)
        uri = _loopback_neo4j_uri(environment["PLAYGROUND_NEO4J_URI"])
        # Validate the name before opening a connection or writing schema.
        request = CreateWorkspace(name=args.name, operation_id=FIRST_OPERATION)
        with GraphDatabase.driver(uri,
                auth=(environment["PLAYGROUND_NEO4J_USER"], environment["PLAYGROUND_NEO4J_PASSWORD"]),
                connection_timeout=5.0, max_connection_pool_size=4) as driver:
            deadline = time.monotonic() + 60
            while True:
                try:
                    driver.verify_connectivity()
                    break
                except ServiceUnavailable:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Neo4j did not become ready within 60 seconds") from None
                    time.sleep(1)
            report = initialize(driver, environment["PLAYGROUND_NEO4J_DATABASE"], request.name, profile)
        print(json.dumps(report, ensure_ascii=False))
        print("Initialization complete. No business sources were imported and no model API was called.")
        return 0
    except Neo4jError as error:
        print("Neo4j initialization failed (" + str(error.code) + "); check the database and private configuration.", file=sys.stderr)
    except (ValueError, RuntimeError, OSError) as error:
        print(str(error), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
