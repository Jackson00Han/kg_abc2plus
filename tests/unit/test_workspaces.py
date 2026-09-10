"""Project boundary, two-role sessions and generation reset checks."""
from copy import deepcopy
import threading
import unittest
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from graphrag_prod.api.auth import AuthenticationError, JWTAuthConfig
from graphrag_prod.api.runtime import ConflictError, OperationEnvelope, OperationKind
from graphrag_prod.playground.runtime import PLAYGROUND_AUDIENCE, PLAYGROUND_ISSUER
from graphrag_prod.playground.workspaces import (
    CreateWorkspace, ResetWorkspace, WorkspaceAuthenticator, WorkspaceError,
    WorkspaceRegistry, attach_workspace_routes,
)

KEY = b"workspace-test-signing-material-0123456789"


def project(name, tenant):
    return dict(knowledge_base_id=str(uuid4()), name=name, name_key=name.casefold(),
                generation=1, active_tenant_id=tenant, admin_groups=["members", "administrators"],
                user_groups=["members"], created_at="2026-09-10", updated_at="2026-09-10")


class MemoryStore:
    def __init__(self):
        self.projects = [project("项目 A", "project-a"), project("项目 B", "project-b")]
        self.operations = {}
        self.archives = []

    def list(self): return deepcopy(self.projects)
    def active(self, tenant): return next((deepcopy(p) for p in self.projects if p["active_tenant_id"] == tenant), None)
    def replay(self, operation, fingerprint):
        if operation not in self.operations: return None
        found, result = self.operations[operation]
        if found != fingerprint: raise WorkspaceError("conflict")
        return deepcopy(result)
    def counts(self, tenant): return dict(documents=0, chunks=0, records=0, ontologies=0, publications=0)
    def save(self, value, *, previous, operation_id, fingerprint, actor):
        if previous:
            index = next(i for i, p in enumerate(self.projects) if p["knowledge_base_id"] == previous["knowledge_base_id"])
            if self.projects[index]["generation"] != previous["generation"]: raise WorkspaceError("conflict")
            self.archives.append(deepcopy(previous)); self.projects[index] = deepcopy(value)
        else: self.projects.append(deepcopy(value))
        self.operations[operation_id] = fingerprint, deepcopy(value)
        return value


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore(); self.prepared = []
        self.registry = WorkspaceRegistry(self.store, KEY, self.prepared.append)
        self.auth = WorkspaceAuthenticator(JWTAuthConfig(issuer=PLAYGROUND_ISSUER,
            audience=PLAYGROUND_AUDIENCE, secret=KEY), self.registry)
        self.a, self.b = self.store.list()
        self.admin_session = self.session(self.a)
        self.admin = self.auth.verify_identity(self.admin_session["access_token"])

    def session(self, value, role="admin"):
        person = self.registry.personas(value)[0 if role == "admin" else 1]
        return self.registry.issue_session(person.persona_id)

    def reset_request(self, value=None, **updates):
        value = value or self.a
        return ResetWorkspace(name=value["name"], expected_generation=value["generation"],
            confirmation="RESET_CURRENT_KNOWLEDGE_BASE", operation_id=str(uuid4()), **updates)

    def test_exactly_admin_and_user_with_separate_capabilities(self):
        people = self.registry.personas(self.a)
        self.assertEqual([p.label for p in people], ["管理员", "用户"])
        self.assertIn("workspace:manage", people[0].scopes)
        self.assertNotIn("knowledge:construct", people[1].scopes)
        self.assertNotIn("knowledge:publish", people[1].scopes)
        self.assertEqual(people[1].groups, ("members",))

    def test_new_project_is_empty_separate_and_creation_is_idempotent(self):
        request = CreateWorkspace(name="独立项目", operation_id=str(uuid4()))
        result = self.registry.create(self.admin, request)
        self.assertNotIn(result["active_tenant_id"], [self.a["active_tenant_id"], self.b["active_tenant_id"]])
        self.assertEqual(self.registry.create(self.admin, request), result)
        self.assertEqual(len(self.prepared), 1)
        self.assertEqual(self.store.projects[:2], [self.a, self.b])

    def test_reset_archives_only_current_project_and_revokes_old_sessions(self):
        result = self.registry.reset(self.admin, self.a["knowledge_base_id"], self.reset_request())
        self.assertEqual(result["generation"], 2)
        self.assertNotEqual(result["active_tenant_id"], self.a["active_tenant_id"])
        self.assertEqual(self.store.projects[1], self.b)
        self.assertEqual(self.store.archives, [self.a])
        with self.assertRaises(AuthenticationError): self.auth.verify_identity(self.admin_session["access_token"])
        with self.assertRaises(KeyError): self.registry.issue_session(self.admin_session["identity"]["id"])
        fresh = self.session(result)
        self.assertEqual(self.auth.verify_identity(fresh["access_token"]).principal.tenant_id, result["active_tenant_id"])

    def test_reset_cannot_cross_project_and_user_cannot_manage(self):
        with self.assertRaises(WorkspaceError) as error:
            self.registry.reset(self.admin, self.b["knowledge_base_id"], self.reset_request(self.b))
        self.assertEqual(error.exception.status, 403)
        user = self.auth.verify_identity(self.session(self.a, "user")["access_token"])
        with self.assertRaises(WorkspaceError): self.registry.create(user, CreateWorkspace(name="bad", operation_id=str(uuid4())))
        with self.assertRaises(WorkspaceError): self.registry.reset(user, self.a["knowledge_base_id"], self.reset_request())
        self.assertEqual(self.prepared, [])

    def test_stale_generation_and_changed_confirmation_are_rejected(self):
        for request in (self.reset_request().model_copy(update={"expected_generation": 2}),
                        self.reset_request().model_copy(update={"name": "其他项目"})):
            with self.assertRaises(WorkspaceError): self.registry.reset(self.admin, self.a["knowledge_base_id"], request)
        self.assertEqual(self.prepared, [])

    def test_lost_response_replay_is_safe_after_acquiring_current_session(self):
        request = self.reset_request()
        result = self.registry.reset(self.admin, self.a["knowledge_base_id"], request)
        fresh = self.auth.verify_identity(self.session(result)["access_token"])
        self.assertEqual(self.registry.reset(fresh, self.a["knowledge_base_id"], request), result)
        self.assertEqual(len(self.store.archives), 1)
        self.assertEqual(len(self.prepared), 1)

    def test_running_worker_blocks_reset_and_queued_old_request_cannot_enter(self):
        entered, finish = threading.Event(), threading.Event()
        class Backend:
            def execute(self, envelope): entered.set(); finish.wait(3); return "ok"
        guarded = self.registry.wrap_backend(Backend())
        envelope = OperationEnvelope(operation=OperationKind.KNOWLEDGE_CONSTRUCT,request_id="r",trace_id="t",
            principal_id=self.admin.principal.principal_id,tenant_id=self.a["active_tenant_id"])
        worker = threading.Thread(target=guarded.execute,args=(envelope,));worker.start()
        try:
            self.assertTrue(entered.wait(1))
            with self.assertRaises(WorkspaceError): self.registry.reset(self.admin,self.a["knowledge_base_id"],self.reset_request())
        finally: finish.set();worker.join(2)
        self.registry.reset(self.admin,self.a["knowledge_base_id"],self.reset_request())
        with self.assertRaises(ConflictError): guarded.execute(envelope)

    def test_background_read_does_not_block_reset_and_cannot_return_stale_data(self):
        entered, finish = threading.Event(), threading.Event()
        failures = []
        class Backend:
            def execute(self, envelope):
                entered.set(); finish.wait(3)
                return "old project data"
        guarded = self.registry.wrap_backend(Backend())
        envelope = OperationEnvelope(operation=OperationKind.GRAPH_QUERY, request_id="read", trace_id="read",
            principal_id=self.admin.principal.principal_id, tenant_id=self.a["active_tenant_id"])
        def read():
            try: guarded.execute(envelope)
            except Exception as error: failures.append(error)
        worker = threading.Thread(target=read); worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertEqual(self.registry.workers[self.a["active_tenant_id"]], 0)
            reset = self.registry.reset(self.admin, self.a["knowledge_base_id"], self.reset_request())
            self.assertEqual(reset["generation"], 2)
        finally:
            finish.set(); worker.join(2)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], ConflictError)

    def test_prepare_failure_leaves_existing_knowledge_and_sessions_valid(self):
        def fail(tenant): raise RuntimeError("index unavailable")
        self.registry.prepare_tenant = fail
        with self.assertRaises(RuntimeError): self.registry.reset(self.admin,self.a["knowledge_base_id"],self.reset_request())
        self.assertEqual(self.store.projects, [self.a,self.b])
        self.auth.verify_identity(self.admin_session["access_token"])

    def test_http_management_requires_bearer_admin_and_explicit_reset(self):
        app = FastAPI();attach_workspace_routes(app,self.registry,self.auth)
        with TestClient(app) as client:
            path = f"/playground/workspaces/{self.a['knowledge_base_id']}/reset"
            self.assertEqual(client.get('/playground/workspaces').status_code,401)
            user = self.session(self.a,"user")
            self.assertEqual(client.post(path,json=self.reset_request().model_dump(),
                headers={"Authorization":"Bearer "+user["access_token"]}).status_code,403)
            headers={"Authorization":"Bearer "+self.admin_session["access_token"]}
            self.assertEqual(client.post(path,json={},headers=headers).status_code,422)
            response=client.post(path,json=self.reset_request().model_dump(),headers=headers)
            self.assertEqual(response.status_code,200)
            self.assertEqual(client.get('/playground/workspaces',headers=headers).status_code,401)


if __name__ == "__main__": unittest.main()
