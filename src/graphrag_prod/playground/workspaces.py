"""Persistent project spaces for the loopback workbench.

A project owns one active tenant namespace. Reset switches to a new namespace;
immutable evidence in previous generations stays archived and cannot be read
with workbench sessions. No document or graph ID is rewritten or shared.
"""
from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from hashlib import sha256
import json
import threading
import time
from typing import Any, Literal
from uuid import UUID, NAMESPACE_URL, uuid5

import jwt
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from neo4j import Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from graphrag_prod.api.auth import AuthenticationError, JWTAuthenticator
from graphrag_prod.api.runtime import ConflictError, OperationKind
from .runtime import (PLAYGROUND_AUDIENCE, PLAYGROUND_ISSUER, PLAYGROUND_SCOPES,
                      PLAYGROUND_TOKEN_LIFETIME_SECONDS, PlaygroundPersona)

MANAGE = "workspace:manage"
READ_SCOPES = ("retrieval:read", "ontology:read", "knowledge:graph:read")
MAX_PROJECTS = 100


class WorkspaceError(ValueError):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


class CreateWorkspace(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=80)
    operation_id: str

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("请输入有效的知识库名称")
        return value

    @field_validator("operation_id")
    @classmethod
    def valid_operation(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("operation_id must be a canonical UUID")
        return value


class ResetWorkspace(CreateWorkspace):
    expected_generation: int = Field(ge=1)
    confirmation: Literal["RESET_CURRENT_KNOWLEDGE_BASE"]


def _fingerprint(kind: str, actor: str, body: dict) -> str:
    return sha256(json.dumps([kind, actor, body], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _project_id(seed: str) -> str:
    return str(uuid5(NAMESPACE_URL, "graphrag-workspace:" + seed))


def prepare_workspace_tenant(driver: Any, database: str, embedder: Any, tenant_id: str) -> None:
    from graphrag_prod.domain.models import ChunkEmbedding
    from graphrag_prod.domain.ids import chunk_embedding_id
    from graphrag_prod.ingestion import Neo4jEmbeddingIndexManager
    # Establish an empty, versioned vector space without calling a model.
    manager = Neo4jEmbeddingIndexManager(driver, database)
    active = manager.active_generation(tenant_id)
    if active is not None:
        if active.embedding_space_id != embedder.embedding_space_id:
            raise RuntimeError("workspace embedding space differs from configured provider")
        return
    profile = ChunkEmbedding(
        embedding_id=chunk_embedding_id("workspace-profile:" + tenant_id, embedder.embedding_space_id),
        tenant_id=tenant_id, chunk_id="workspace-profile:" + tenant_id,
        embedding_space_id=embedder.embedding_space_id, provider=embedder.provider,
        model=embedder.model, revision=embedder.revision, dimensions=embedder.dimensions,
        normalization=embedder.normalization, created_at=datetime.now(UTC),
        vector=(1.0,) + (0.0,) * (embedder.dimensions - 1),
    )
    target = manager.prepare(tenant_id=tenant_id, embedding_profile=profile, generation_version=1)
    manager.activate(target.generation_id, expected_active_generation_id=None)


class Neo4jWorkspaceStore:
    def __init__(self, driver: Any, database: str, *, pump_only: bool = False):
        self.driver, self.database = driver, database
        self.pump_only = pump_only

    def rows(self, text: str, **parameters: Any) -> list[dict]:
        with self.driver.session(database=self.database) as session:
            return [dict(row) for row in session.run(Query(text, timeout=5.0), **parameters)]

    def initialize(self, personas: tuple[PlaygroundPersona, ...]) -> None:
        for field in ("knowledge_base_id", "name_key", "active_tenant_id"):
            self.rows(f"CREATE CONSTRAINT workbench_workspace_{field}_unique IF NOT EXISTS "
                      f"FOR (w:WorkbenchKnowledgeBase) REQUIRE w.{field} IS UNIQUE")
        self.rows("CREATE CONSTRAINT workbench_workspace_operation_unique IF NOT EXISTS "
                  "FOR (o:WorkbenchWorkspaceOperation) REQUIRE o.operation_id IS UNIQUE")
        names = {"industrial-schneider-demo": "工业知识库", "tenant-alpha": "示例项目 A",
                 "tenant-beta": "示例项目 B", "demo-a": "一号泵站", "demo-b": "二号泵站"}
        for tenant in sorted({p.tenant_id for p in personas}):
            people = [p for p in personas if p.tenant_id == tenant]
            groups = sorted({group for p in people for group in p.groups})
            public = [group for group in groups if group == "public" or group.endswith("-public")]
            name = names.get(tenant, tenant)
            self.rows("""MERGE (w:WorkbenchKnowledgeBase {knowledge_base_id:$id})
                ON CREATE SET w.name=$name, w.name_key=$name_key, w.active_tenant_id=$tenant,
                  w.generation=1, w.admin_groups=$groups, w.user_groups=$public,
                  w.created_at=$now, w.updated_at=$now, w.initial_tenant_id=$tenant
                RETURN w.knowledge_base_id AS id""", id=_project_id(tenant), name=name,
                name_key=name.casefold(), tenant=tenant, groups=groups, public=public or groups[:1],
                now=datetime.now(UTC).isoformat())

    def visible(self, project: dict) -> bool:
        return not self.pump_only or (
            project.get("initial_tenant_id") is None or
            (project.get("initial_tenant_id") == "industrial-schneider-demo" and
             project["active_tenant_id"] != "industrial-schneider-demo")
        )

    def list(self) -> list[dict]:
        return [row["project"] for row in self.rows("""MATCH (w:WorkbenchKnowledgeBase)
            RETURN properties(w) AS project ORDER BY w.created_at, w.knowledge_base_id LIMIT 101""")
            if self.visible(row["project"])]

    def get(self, project_id: str) -> dict:
        rows = self.rows("MATCH (w:WorkbenchKnowledgeBase {knowledge_base_id:$id}) "
                         "RETURN properties(w) AS project", id=project_id)
        if not rows or not self.visible(rows[0]["project"]):
            raise WorkspaceError("知识库不存在，请刷新列表。", 404)
        return rows[0]["project"]

    def active(self, tenant: str) -> dict | None:
        rows = self.rows("MATCH (w:WorkbenchKnowledgeBase {active_tenant_id:$tenant}) "
                         "RETURN properties(w) AS project", tenant=tenant)
        return rows[0]["project"] if rows and self.visible(rows[0]["project"]) else None

    def replay(self, operation_id: str, fingerprint: str) -> dict | None:
        rows = self.rows("MATCH (o:WorkbenchWorkspaceOperation {operation_id:$id}) "
                         "RETURN o.fingerprint AS fingerprint, o.result_json AS result", id=operation_id)
        if not rows:
            return None
        if rows[0]["fingerprint"] != fingerprint:
            raise WorkspaceError("操作编号已用于不同请求，请刷新后重试。")
        return json.loads(rows[0]["result"])

    def save(self, project: dict, *, previous: dict | None, operation_id: str, fingerprint: str, actor: str) -> dict:
        def write(tx):
            op = tx.run("MERGE (o:WorkbenchWorkspaceOperation {operation_id:$id}) "
                        "ON CREATE SET o.fingerprint=$fingerprint "
                        "SET o.lock_version=coalesce(o.lock_version,0)+1 "
                        "RETURN o.fingerprint AS fingerprint, o.result_json AS result",
                        id=operation_id, fingerprint=fingerprint).single()
            if op["fingerprint"] != fingerprint:
                raise WorkspaceError("操作编号已用于不同请求。")
            if op["result"]:
                return json.loads(op["result"])
            if previous:
                row = tx.run("""MATCH (w:WorkbenchKnowledgeBase {knowledge_base_id:$id})
                    SET w.lock_version=coalesce(w.lock_version,0)+1
                    WITH w WHERE w.generation=$generation AND w.active_tenant_id=$tenant
                    SET w += $next
                    CREATE (a:WorkbenchKnowledgeBaseArchive {knowledge_base_id:$id,
                        tenant_id:$tenant,generation:$generation,archived_at:$now,archived_by:$actor,
                        operation_id:$operation})
                    RETURN w.knowledge_base_id AS id""", id=previous["knowledge_base_id"],
                    generation=previous["generation"], tenant=previous["active_tenant_id"],
                    next=project, now=project["updated_at"], actor=actor, operation=operation_id).single()
                if not row:
                    raise WorkspaceError("知识库已发生变化，请重新核对后重置。")
            else:
                if tx.run("MATCH (w:WorkbenchKnowledgeBase {name_key:$name}) RETURN w.name AS name",
                          name=project["name_key"]).single():
                    raise WorkspaceError("已有同名知识库，请使用其他名称。")
                tx.run("CREATE (w:WorkbenchKnowledgeBase) SET w=$project", project=project).consume()
            tx.run("MATCH (o:WorkbenchWorkspaceOperation {operation_id:$id}) "
                   "SET o.result_json=$result, o.actor=$actor, o.completed_at=$now",
                   id=operation_id, result=json.dumps(project), actor=actor, now=project["updated_at"]).consume()
            return project
        with self.driver.session(database=self.database) as session:
            return session.execute_write(write)

    def counts(self, tenant: str) -> dict:
        rows = self.rows("""MATCH (n) WHERE n.tenant_id=$tenant
            RETURN count(CASE WHEN n:Document THEN 1 END) AS documents,
                   count(CASE WHEN n:Chunk THEN 1 END) AS chunks,
                   count(CASE WHEN n:KnowledgeRecordHead THEN 1 END) AS records,
                   count(CASE WHEN n:TBoxVersion THEN 1 END) AS ontologies,
                   count(CASE WHEN n:KnowledgePublication THEN 1 END) AS publications""", tenant=tenant)
        return rows[0]


class WorkspaceRegistry:
    """Local server's control plane; actual graph APIs keep tenant/ACL enforcement."""
    def __init__(self, store: Neo4jWorkspaceStore, signing_key: bytes, prepare_tenant):
        self.store, self.signing_key, self.prepare_tenant = store, signing_key, prepare_tenant
        self.lock = threading.RLock()
        self.workers: Counter[str] = Counter()  # Mutations only; background reads must not block reset.

    def personas(self, project: dict) -> tuple[PlaygroundPersona, ...]:
        key = project["knowledge_base_id"]
        return tuple(PlaygroundPersona(f"workspace-{key}-{project['generation']}-{role}", f"workspace-{key}-{role}", label,
            project["active_tenant_id"], tuple(project[f"{role}_groups"]), scopes)
            for role, label, scopes in (("admin", "管理员", PLAYGROUND_SCOPES + (MANAGE,)),
                                        ("user", "用户", READ_SCOPES)))

    def bootstrap(self) -> dict:
        projects = self.store.list()
        return {"knowledge_bases": [{key: p[key] for key in ("knowledge_base_id", "name", "generation")}
                                    for p in projects],
                "personas": [{**person.as_dict(), "knowledge_base_id": p["knowledge_base_id"], "role": role,
                              "generation": p["generation"]}
                             for p in projects for role, person in zip(("admin", "user"), self.personas(p))],
                "default_knowledge_base_id": next((p["knowledge_base_id"] for p in projects
                    if p.get("initial_tenant_id") == "industrial-schneider-demo"), projects[0]["knowledge_base_id"])}

    def issue_session(self, persona_id: str) -> dict:
        for p in self.store.list():
            for role, person in zip(("admin", "user"), self.personas(p)):
                if person.persona_id != persona_id:
                    continue
                now = int(time.time()); expires = now + PLAYGROUND_TOKEN_LIFETIME_SECONDS
                token = jwt.encode({"iss": PLAYGROUND_ISSUER, "aud": PLAYGROUND_AUDIENCE,
                    "sub": person.principal_id, "tenant_id": person.tenant_id, "groups": list(person.groups),
                    "scope": " ".join(person.scopes), "iat": now, "exp": expires}, self.signing_key, algorithm="HS256")
                return {"access_token": token, "token_type": "Bearer", "expires_at": expires,
                        "identity": {**person.as_dict(), "knowledge_base_id": p["knowledge_base_id"],
                                     "role": role, "generation": p["generation"]}}
        raise KeyError("unknown workspace identity")

    def require_admin(self, identity, project_id: str | None = None) -> dict:
        current = self.store.active(identity.principal.tenant_id)
        if current is None:
            raise WorkspaceError("知识库已重置，请刷新页面重新进入。", 401)
        if MANAGE not in identity.scopes or (project_id and current["knowledge_base_id"] != project_id):
            raise WorkspaceError("只有当前知识库的管理员可以执行此操作。", 403)
        return current

    def create(self, identity, request: CreateWorkspace) -> dict:
        with self.lock:
            self.require_admin(identity)
            actor = identity.principal.principal_id
            fingerprint = _fingerprint("create", actor, request.model_dump())
            replay = self.store.replay(request.operation_id, fingerprint)
            if replay: return replay
            projects = self.store.list()
            if len(projects) >= MAX_PROJECTS: raise WorkspaceError("当前服务最多管理 100 个知识库。")
            if any(p["name_key"] == request.name.casefold() for p in projects):
                raise WorkspaceError("已有同名知识库，请使用其他名称。")
            key = _project_id(request.operation_id)
            tenant = "workspace-" + key
            self.prepare_tenant(tenant)
            now = datetime.now(UTC).isoformat()
            project = dict(knowledge_base_id=key, name=request.name, name_key=request.name.casefold(),
                active_tenant_id=tenant, generation=1, admin_groups=["members", "administrators"],
                user_groups=["members"], created_at=now, updated_at=now)
            return self.store.save(project, previous=None, operation_id=request.operation_id,
                                   fingerprint=fingerprint, actor=actor)

    def reset(self, identity, project_id: str, request: ResetWorkspace) -> dict:
        with self.lock:
            previous = self.require_admin(identity, project_id)
            actor = identity.principal.principal_id
            fingerprint = _fingerprint("reset:" + project_id, actor, request.model_dump())
            replay = self.store.replay(request.operation_id, fingerprint)
            if replay: return replay
            if previous["generation"] != request.expected_generation or previous["name"] != request.name:
                raise WorkspaceError("知识库名称或版本已变化，请重新打开重置面板。")
            if self.workers[previous["active_tenant_id"]]:
                raise WorkspaceError("当前知识库仍有写入任务运行，请等待上传、审核或发布结束后再重置。")
            tenant = "workspace-" + _project_id(request.operation_id)
            self.prepare_tenant(tenant)
            project = {**previous, "active_tenant_id": tenant, "generation": previous["generation"] + 1,
                       "admin_groups": ["members", "administrators"], "user_groups": ["members"],
                       "updated_at": datetime.now(UTC).isoformat()}
            return self.store.save(project, previous=previous, operation_id=request.operation_id,
                                   fingerprint=fingerprint, actor=actor)

    def wrap_backend(self, backend):
        registry = self
        class GuardedBackend:
            def execute(self, envelope, /):
                if envelope.operation in {OperationKind.HEALTH, OperationKind.READINESS}:
                    return backend.execute(envelope)
                with registry.lock:
                    if registry.store.active(envelope.tenant_id) is None:
                        raise ConflictError("知识库已重置，请刷新页面。")
                    if envelope.operation.is_write:
                        registry.workers[envelope.tenant_id] += 1
                try:
                    result = backend.execute(envelope)
                    with registry.lock:
                        if registry.store.active(envelope.tenant_id) is None:
                            raise ConflictError("知识库已重置，请刷新页面。")
                    return result
                finally:
                    if envelope.operation.is_write:
                        with registry.lock:
                            registry.workers[envelope.tenant_id] -= 1
        return GuardedBackend()


class WorkspaceAuthenticator(JWTAuthenticator):
    def __init__(self, config, registry: WorkspaceRegistry):
        super().__init__(config)
        self.registry = registry

    def verify_identity(self, token):
        identity = super().verify_identity(token)
        if self.registry.store.active(identity.principal.tenant_id) is None:
            raise AuthenticationError("workspace session expired")
        return identity


def attach_workspace_routes(app: FastAPI, registry: WorkspaceRegistry, authenticator: WorkspaceAuthenticator) -> None:
    def identity(request: Request):
        from graphrag_prod.api.app import extract_bearer_token
        try:
            return authenticator.verify_identity(extract_bearer_token(request.headers.get("authorization", "")))
        except AuthenticationError:
            raise WorkspaceError("请刷新当前知识库身份后重试。", 401) from None

    @app.exception_handler(WorkspaceError)
    async def workspace_error(request, error):
        return JSONResponse({"error": {"code": "WORKSPACE_CONFLICT", "message": str(error)}},
                            status_code=error.status, headers={"Cache-Control": "no-store"})

    @app.get("/playground/workspaces", include_in_schema=False)
    def workspace_list(request: Request):
        identity(request)
        return registry.bootstrap()

    @app.post("/playground/workspaces", include_in_schema=False)
    def workspace_create(request: Request, body: CreateWorkspace):
        project = registry.create(identity(request), body)
        return {"knowledge_base": project, **registry.bootstrap()}

    @app.get("/playground/workspaces/{project_id}/reset-preview", include_in_schema=False)
    def reset_preview(project_id: str, request: Request):
        with registry.lock:
            project = registry.require_admin(identity(request), project_id)
            return {"knowledge_base_id": project_id, "name": project["name"],
                    "generation": project["generation"], "counts": registry.store.counts(project["active_tenant_id"]),
                    "running_tasks": registry.workers[project["active_tenant_id"]]}

    @app.post("/playground/workspaces/{project_id}/reset", include_in_schema=False)
    def workspace_reset(project_id: str, request: Request, body: ResetWorkspace):
        project = registry.reset(identity(request), project_id, body)
        return {"knowledge_base": project, **registry.bootstrap()}
