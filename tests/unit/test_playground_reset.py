"""Local reset admission, CSRF, replay and late-worker regression checks."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import unittest
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from graphrag_prod.api.app import APISettings, create_app
from graphrag_prod.api.auth import JWTAuthConfig, JWTAuthenticator
from graphrag_prod.api.runtime import (
    BackendResult, BoundedOperationRunner, ConflictError, DependencyTimeoutError,
    OperationEnvelope, OperationKind, RuntimePolicy,
)
from graphrag_prod.playground.reset import (
    PlaygroundResetController, ResetControlError, ResetRequest,
)
from graphrag_prod.playground.runtime import PlaygroundCatalog, attach_playground_routes
from tests.fixtures.dev_corpus import load_dev_corpus_fixture


_TOKEN = "local-reset-test-token-32-bytes-minimum"
_URL = "http://127.0.0.1:8002"
_KEY = b"local-playground-test-signing-key-32-bytes"


def _command(controller, *, operation_id=None, generation=None):
    return ResetRequest(
        confirmation="RESET_ALL", operation_id=operation_id or str(uuid4()),
        expected_generation=generation or controller.status()["generation"],
    )


def _wait_terminal(controller):
    deadline = time.monotonic() + 3
    while controller.status()["state"] == "RUNNING":
        if time.monotonic() >= deadline:
            raise AssertionError("reset worker did not finish")
        time.sleep(0.005)
    return controller.status()


def _envelope(request_id):
    return OperationEnvelope(
        operation=OperationKind.ONTOLOGY_LIST, request_id=request_id,
        trace_id="reset-unit-trace", principal_id="unit-user", tenant_id="tenant-alpha",
        scopes=frozenset({"ontology:read"}),
    )


class PlaygroundResetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = load_dev_corpus_fixture()
        cls.catalog = PlaygroundCatalog(cls.fixture, _KEY)

    def _app(self, callback=None):
        controller = PlaygroundResetController(callback or Mock(), control_token=_TOKEN)
        app = FastAPI()

        @app.api_route("/v1/probe", methods=["GET", "POST"])
        async def probe(request: Request):
            return {"request_id": request.headers["x-request-id"]}

        @app.get("/health/live")
        async def live():
            return {"status": "alive"}

        @app.get("/health/ready")
        async def ready():
            return {"status": "ready"}

        attach_playground_routes(app, self.catalog, reset_controller=controller)
        return controller, app

    def test_control_is_opt_in_inert_and_generations_are_not_reused_after_restart(self):
        callback = Mock()
        first, app = self._app(callback)
        second = PlaygroundResetController(callback)
        self.assertNotEqual(first.status()["generation"], second.status()["generation"])
        UUID(first.status()["generation"])
        with TestClient(app, base_url=_URL) as client:
            public = client.get("/playground/bootstrap").json()["local_reset"]
            self.assertEqual(public["state"], "READY")
            self.assertEqual(public["control_token"], _TOKEN)
            status = client.get("/playground/reset", headers={
                "X-Playground-Reset-Token": _TOKEN,
            })
            self.assertEqual(status.status_code, 200)
            self.assertNotIn("control_token", status.json())
            self.assertEqual(status.headers["cache-control"], "no-store")
        callback.assert_not_called()
        plain = FastAPI()
        attach_playground_routes(plain, self.catalog)
        with TestClient(plain) as client:
            self.assertNotIn("local_reset", client.get("/playground/bootstrap").json())
            self.assertEqual(client.post("/playground/reset", json={}).status_code, 404)

    def test_reset_requires_local_host_exact_origin_and_current_control_token(self):
        callback = Mock()
        controller, app = self._app(callback)
        valid = {"X-Playground-Reset-Token": _TOKEN, "Origin": _URL}
        variants = (
            {}, {"Origin": _URL}, {"X-Playground-Reset-Token": _TOKEN},
            {**valid, "Origin": "null"}, {**valid, "Origin": "https://127.0.0.1:8002"},
            {**valid, "Origin": "http://127.0.0.1:8003"},
            {**valid, "Origin": "http://127.0.0.1:8002/"},
            {**valid, "Origin": "http://evil.test:8002"},
            {**valid, "Host": "evil.test:8002", "Origin": "http://evil.test:8002"},
            {**valid, "Host": "192.0.2.1:8002", "Origin": "http://192.0.2.1:8002"},
            {**valid, "Host": "user@127.0.0.1:8002"},
            {**valid, "X-Playground-Reset-Token": "wrong"},
            [("X-Playground-Reset-Token", _TOKEN), ("X-Playground-Reset-Token", _TOKEN),
             ("Origin", _URL)],
            [("X-Playground-Reset-Token", _TOKEN), ("Origin", _URL), ("Origin", _URL)],
        )
        with TestClient(app, base_url=_URL) as client:
            for headers in variants:
                with self.subTest(headers=headers):
                    response = client.post("/playground/reset", headers=headers,
                                           json=_command(controller).model_dump())
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(response.json()["error"]["code"], "PLAYGROUND_RESET_FORBIDDEN")
            self.assertEqual(client.get("/playground/reset").status_code, 403)
        callback.assert_not_called()

    def test_reset_requires_strict_small_json_and_explicit_confirmation(self):
        callback = Mock()
        controller, app = self._app(callback)
        valid = _command(controller).model_dump()
        bad = (
            {}, [], {**valid, "confirmation": True}, {**valid, "confirmation": "RESET"},
            {**valid, "expected_generation": 0}, {**valid, "operation_id": "bad-id"},
            {**valid, "operation_id": valid["operation_id"] + "\n"},
            {**valid, "extra": True}, {**valid, "expected_generation": None},
        )
        headers = {"X-Playground-Reset-Token": _TOKEN, "Origin": _URL}
        with TestClient(app, base_url=_URL) as client:
            for payload in bad:
                with self.subTest(payload=payload):
                    self.assertEqual(client.post("/playground/reset", headers=headers,
                                                 json=payload).status_code, 422)
            for body, content_type in (
                ("{", "application/json"), ("x" * 2_049, "application/json"),
                (json.dumps(valid), "text/plain"),
                (json.dumps(valid)[:-1] + ', "confirmation":"RESET_ALL"}', "application/json"),
            ):
                response = client.post("/playground/reset", content=body,
                                       headers={**headers, "Content-Type": content_type})
                self.assertEqual(response.status_code, 422)
        callback.assert_not_called()

    def test_reset_state_replay_and_new_generation_are_visible_without_repeated_work(self):
        entered, finish = threading.Event(), threading.Event()

        def callback():
            entered.set()
            self.assertTrue(finish.wait(3))

        callback = Mock(side_effect=callback)
        controller, app = self._app(callback)
        command = _command(controller)
        headers = {"X-Playground-Reset-Token": _TOKEN, "Origin": _URL}
        try:
            with TestClient(app, base_url=_URL) as client:
                response = client.post("/playground/reset", headers=headers, json=command.model_dump())
                self.assertEqual(response.status_code, 202)
                self.assertTrue(entered.wait(1))
                running = response.json()
                self.assertEqual(running["state"], "RUNNING")
                self.assertEqual(running["reset_id"], command.operation_id)
                self.assertNotEqual(running["generation"], command.expected_generation)
                self.assertEqual(client.post("/playground/reset", headers=headers,
                                             json=command.model_dump()).json(), running)
                self.assertEqual(client.get("/health/live").status_code, 200)
                for path in ("/v1/probe", "/health/ready"):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response.json()["error"]["code"], "PLAYGROUND_RESET_RUNNING")
                self.assertEqual(client.post("/playground/reset", headers=headers,
                    json=_command(controller).model_dump()).status_code, 409)
                finish.set()
                succeeded = _wait_terminal(controller)
                self.assertEqual(succeeded["state"], "SUCCEEDED")
                self.assertEqual(controller.start(command), succeeded)
                with self.assertRaises(ResetControlError):
                    controller.start(_command(controller, operation_id=command.operation_id))
                self.assertEqual(client.get("/v1/probe").status_code, 200)
        finally:
            finish.set()
        callback.assert_called_once()

    def test_failed_reset_stays_closed_hides_details_and_requires_fresh_confirmation(self):
        callback = Mock(side_effect=[RuntimeError("credential-and-private-source"), None])
        controller, app = self._app(callback)
        first = _command(controller)
        controller.start(first)
        failed = _wait_terminal(controller)
        self.assertEqual(failed["state"], "FAILED")
        self.assertEqual(failed["error_code"], "PLAYGROUND_RESET_FAILED")
        self.assertNotIn("credential", json.dumps(controller.bootstrap()))
        self.assertEqual(controller.start(first), failed)
        callback.assert_called_once()
        with TestClient(app, base_url=_URL) as client:
            response = client.get("/v1/probe")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["error"]["code"], "PLAYGROUND_RESET_FAILED")
        controller.start(_command(controller))
        self.assertEqual(_wait_terminal(controller)["state"], "SUCCEEDED")
        self.assertEqual(callback.call_count, 2)
        # A replay of the original failed reset cannot destroy the new result.
        self.assertEqual(controller.start(first), failed)
        self.assertEqual(callback.call_count, 2)

    def test_middleware_enforces_generation_and_owns_unique_request_identifiers(self):
        controller, app = self._app()
        generation = controller.status()["generation"]
        with TestClient(app, base_url=_URL) as client:
            for header in ({}, {"X-Playground-Generation": str(uuid4())}):
                response = client.post("/v1/probe", headers=header)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()["error"]["code"], "PLAYGROUND_RESET_STALE")
            headers = {"X-Playground-Generation": generation, "X-Request-ID": "client-reused-id"}
            first = client.post("/v1/probe", headers=headers).json()["request_id"]
            second = client.post("/v1/probe", headers=headers).json()["request_id"]
            self.assertNotEqual(first, second)
            self.assertNotEqual(first, "client-reused-id")
            self.assertEqual(controller._requests, {})
            controller.start(_command(controller))
            _wait_terminal(controller)
            self.assertEqual(client.post("/v1/probe", headers=headers).status_code, 409)
            headers["X-Playground-Generation"] = controller.status()["generation"]
            self.assertEqual(client.post("/v1/probe", headers=headers).status_code, 200)

    def test_active_admission_or_real_worker_prevents_reset_after_http_completion(self):
        callback = Mock()
        controller = PlaygroundResetController(callback)
        request_id = controller.admit(write=False, generation=None)
        with self.assertRaises(ResetControlError) as caught:
            controller.start(_command(controller))
        self.assertEqual(caught.exception.code, "PLAYGROUND_RESET_BUSY")
        entered, finish = threading.Event(), threading.Event()

        def execute(_):
            entered.set()
            if not finish.wait(3):
                raise AssertionError("worker did not finish")
            return BackendResult(payload={})

        backend = controller.wrap_backend(Mock(execute=Mock(side_effect=execute)))
        thread = threading.Thread(target=backend.execute, args=(_envelope(request_id),))
        try:
            thread.start()
            self.assertTrue(entered.wait(1))
            controller.release(request_id)
            self.assertFalse(controller._requests)
            with self.assertRaises(ResetControlError) as caught:
                controller.start(_command(controller))
            self.assertEqual(caught.exception.code, "PLAYGROUND_RESET_BUSY")
            callback.assert_not_called()
        finally:
            finish.set()
            thread.join(3)
        controller.start(_command(controller))
        self.assertEqual(_wait_terminal(controller)["state"], "SUCCEEDED")
        with self.assertRaises(ConflictError):
            backend.execute(_envelope(request_id))

    def test_actual_runner_cancellation_cannot_replay_queued_old_work(self):
        async def scenario():
            controller = PlaygroundResetController(Mock())
            first_id = controller.admit(write=False, generation=None)
            second_id = controller.admit(write=False, generation=None)
            entered, finish = threading.Event(), threading.Event()
            calls = []

            def execute(envelope):
                calls.append(envelope.request_id)
                entered.set()
                if not finish.wait(3):
                    raise AssertionError("runner was not released")
                return BackendResult(payload={})

            runner = BoundedOperationRunner(controller.wrap_backend(Mock(execute=execute)),
                policy=RuntimePolicy(max_workers=1, max_queue_size=1, timeout_seconds=2, max_attempts=1))
            first = asyncio.create_task(runner.run(_envelope(first_id)))
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            second = asyncio.create_task(runner.run(_envelope(second_id)))
            try:
                await asyncio.sleep(0.02)
                second.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await second
                controller.release(second_id)
                controller.release(first_id)
                with self.assertRaises(ResetControlError):
                    controller.start(_command(controller))
                finish.set()
                await first
                controller.start(_command(controller))
                await asyncio.to_thread(_wait_terminal, controller)
                await runner.aclose()
                self.assertEqual(calls, [first_id])
            finally:
                finish.set()
                await runner.aclose()
        asyncio.run(scenario())

    def test_actual_runner_timeout_does_not_release_the_reset_worker_guard(self):
        async def scenario():
            controller = PlaygroundResetController(Mock())
            request_id = controller.admit(write=False, generation=None)
            finish = threading.Event()

            def execute(_):
                if not finish.wait(3):
                    raise AssertionError("timed-out worker was not released")
                return BackendResult(payload={})

            runner = BoundedOperationRunner(controller.wrap_backend(Mock(execute=execute)),
                policy=RuntimePolicy(max_workers=1, max_queue_size=0, timeout_seconds=0.05, max_attempts=1))
            try:
                with self.assertRaises(DependencyTimeoutError):
                    await runner.run(_envelope(request_id))
                controller.release(request_id)
                with self.assertRaises(ResetControlError) as caught:
                    controller.start(_command(controller))
                self.assertEqual(caught.exception.code, "PLAYGROUND_RESET_BUSY")
                finish.set()
                await runner.aclose()
                controller.start(_command(controller))
                await asyncio.to_thread(_wait_terminal, controller)
            finally:
                finish.set()
                await runner.aclose()
        asyncio.run(scenario())

    def test_gate_wraps_production_api_without_bypassing_jwt_authorization(self):
        raw_backend = Mock(execute=Mock(return_value=BackendResult(payload={"items": []})))
        controller = PlaygroundResetController(Mock())
        app = create_app(
            authenticator=JWTAuthenticator(JWTAuthConfig(
                issuer="sample-graphrag-local-playground", audience="sample-graphrag-local-api", secret=_KEY)),
            backend=controller.wrap_backend(raw_backend),
            settings=APISettings(service_name="reset-unit"),
        )
        attach_playground_routes(app, self.catalog, reset_controller=controller)
        with TestClient(app, base_url=_URL) as client:
            self.assertEqual(client.get("/v1/ontologies").status_code, 401)
            raw_backend.execute.assert_not_called()
            self.assertEqual(controller._requests, {})
            persona = next(item for item in self.catalog.personas
                           if item.groups == ("alpha-public",))
            session = self.catalog.issue_session(persona.persona_id)
            headers = {
                "Authorization": "Bearer " + session["access_token"],
                "X-Playground-Generation": controller.status()["generation"],
                "X-Request-ID": "client-untrusted-identifier",
            }
            response = client.get("/v1/ontologies", headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            envelope = raw_backend.execute.call_args.args[0]
            self.assertEqual(envelope.tenant_id, persona.tenant_id)
            self.assertEqual(envelope.access_groups, frozenset(persona.groups))
            self.assertNotEqual(envelope.request_id, "client-untrusted-identifier")
            self.assertEqual(controller._requests, {})
            self.assertEqual(controller._workers, 0)
            response = client.post("/v1/ontologies:import", headers=headers,
                json=self.catalog.bootstrap()["defaults"]["industrial_tbox_template"])
            self.assertEqual(response.status_code, 403, response.text)
            raw_backend.execute.assert_called_once()
            # Backend exceptions also release real-worker accounting.
            identifier = controller.admit(write=False, generation=None)
            raw_backend.execute.side_effect = RuntimeError("private-error")
            with self.assertRaises(RuntimeError):
                controller.wrap_backend(raw_backend).execute(_envelope(identifier))
            controller.release(identifier)
            self.assertEqual(controller._workers, 0)

    def test_worker_start_failure_is_closed_and_replay_is_still_idempotent(self):
        callback = Mock()
        controller = PlaygroundResetController(callback)
        command = _command(controller)
        with patch("graphrag_prod.playground.reset.threading.Thread.start", side_effect=RuntimeError("private")):
            result = controller.start(command)
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(controller.start(command), result)
        with self.assertRaises(ResetControlError):
            controller.admit(write=False, generation=None)
        callback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
