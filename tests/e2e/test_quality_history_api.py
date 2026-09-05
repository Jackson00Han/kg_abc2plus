"""Authenticated HTTP flow for explicitly recorded graph quality audits."""

from __future__ import annotations

import time
import unittest

from fastapi.testclient import TestClient
import jwt

from graphrag_prod.api import (
    GraphRAGApplicationBackend,
    JWTAuthConfig,
    JWTAuthenticator,
    RuntimePolicy,
    create_app,
)
from graphrag_prod.graph.published_quality_history import PublishedGraphQualityHistoryUnavailable
from tests.e2e.test_knowledge_api import (
    AUDIENCE,
    ISSUER,
    SECRET,
    _Documents,
    _Queries,
    _Readiness,
)
from tests.unit.test_api_quality_history import _History, _adapter


PATH = "/v1/knowledge/quality/runs"


def _headers(*, tenant: str = "tenant-alpha", subject: str = "expert-first", scope: str = "knowledge:quality") -> dict[str, str]:
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "iat": now,
            "exp": now + 300,
            "sub": subject,
            "tenant_id": tenant,
            "groups": ["engineers"],
            "scope": scope,
        },
        SECRET,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


class QualityHistoryHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.history = _History()
        app = create_app(
            authenticator=JWTAuthenticator(JWTAuthConfig(issuer=ISSUER, audience=AUDIENCE, secret=SECRET)),
            backend=GraphRAGApplicationBackend(
                documents=_Documents(), queries=_Queries(), readiness=_Readiness(),
                knowledge=_adapter(self.history),
            ),
            runtime_policy=RuntimePolicy(max_attempts=3, initial_backoff_seconds=0, max_backoff_seconds=0),
        )
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_record_list_get_and_explicit_replay_preserve_first_observer(self) -> None:
        headers = _headers()
        # The existing live GET audit remains read-only for audit history.
        live = self.client.get("/v1/knowledge/quality", headers=headers)
        self.assertEqual(live.status_code, 200, live.text)
        self.assertEqual(self.history.calls, [])
        first = self.client.post(PATH, headers=headers, json={})
        self.assertEqual(first.status_code, 200, first.text)
        run = first.json()
        run_id = run["report"]["run_id"]
        listed = self.client.get(PATH, headers=headers, params={"publication_id": "publication-1", "limit": 1})
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()["items"][0]["run_id"], run_id)
        fetched = self.client.get(f"{PATH}/{run_id}", headers=headers)
        self.assertEqual(fetched.status_code, 200, fetched.text)
        self.assertEqual(fetched.json(), run)
        replay = self.client.post(PATH, headers=_headers(subject="expert-second"), json={})
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json(), run)
        self.assertEqual(replay.json()["recorded_by"], "expert-first")
        for forbidden in ("tenant_id", "source_text", "quoted_text", "evidence_text"):
            self.assertNotIn(forbidden, first.text)
        self.assertEqual([call[0] for call in self.history.calls], ["record", "list", "get", "record"])

    def test_unauthenticated_and_missing_scope_requests_never_reach_history(self) -> None:
        for method, path in (("post", PATH), ("get", PATH), ("get", f"{PATH}/run-1")):
            for headers, expected in (({}, 401), (_headers(scope="knowledge:review"), 403)):
                with self.subTest(method=method, status=expected):
                    kwargs = {"headers": headers}
                    if method == "post":
                        kwargs["json"] = {}
                    response = getattr(self.client, method)(path, **kwargs)
                    self.assertEqual(response.status_code, expected, response.text)
        self.assertEqual(self.history.calls, [])

    def test_foreign_tenant_cannot_read_a_recorded_run(self) -> None:
        record = self.client.post(PATH, headers=_headers(), json={})
        self.assertEqual(record.status_code, 200, record.text)
        run_id = record.json()["report"]["run_id"]
        foreign = _headers(tenant="tenant-beta")
        fetched = self.client.get(f"{PATH}/{run_id}", headers=foreign)
        listed = self.client.get(PATH, headers=foreign)
        self.assertEqual(fetched.status_code, 404, fetched.text)
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()["items"], [])
        self.assertNotIn(run_id, fetched.text)

    def test_server_owned_inputs_and_identifier_bounds_reject_invalid_requests(self) -> None:
        headers = _headers()
        for body in ({"tenant_id": "tenant-beta"}, {"limit": 1}, {"publication_id": "publication-1"}):
            with self.subTest(body=body):
                response = self.client.post(PATH, headers=headers, json=body)
                self.assertEqual(response.status_code, 422, response.text)
        for params in ({"limit": 0}, {"limit": 51}, {"limit": "true"}, {"publication_id": ""}, {"publication_id": "bad id"}):
            with self.subTest(params=params):
                response = self.client.get(PATH, headers=headers, params=params)
                self.assertEqual(response.status_code, 422, response.text)
        for identifier in ("bad%20id", "x" * 257):
            response = self.client.get(f"{PATH}/{identifier}", headers=headers)
            self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.history.calls, [])

    def test_record_timeout_and_unavailability_are_not_automatically_retried(self) -> None:
        for failure, expected in ((TimeoutError("sensitive history endpoint"), 504), (PublishedGraphQualityHistoryUnavailable(), 503)):
            with self.subTest(failure=type(failure).__name__):
                self.history.failure = failure
                before = len(self.history.calls)
                response = self.client.post(PATH, headers=_headers(), json={})
                self.assertEqual(response.status_code, expected, response.text)
                self.assertEqual(len(self.history.calls) - before, 1)
                self.assertNotIn("sensitive history endpoint", response.text)


if __name__ == "__main__":
    unittest.main()
