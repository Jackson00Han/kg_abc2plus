"""Unit checks for the local deterministic GraphRAG Playground."""

from __future__ import annotations

from dataclasses import asdict
import time
from types import SimpleNamespace
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
import jwt

from graphrag_prod.playground import (
    PLAYGROUND_AUDIENCE,
    PLAYGROUND_ISSUER,
    PLAYGROUND_RETRIEVAL_LIMITS,
    PLAYGROUND_TOKEN_LIFETIME_SECONDS,
    FixtureQueryEmbedder,
    PlaygroundCatalog,
    attach_playground_routes,
    require_loopback_host,
)
from tests.fixtures.dev_corpus import load_dev_corpus_fixture
from scripts.run_playground import _OpenAICompatibleEmbedder


_SIGNING_KEY = b"local-playground-unit-test-signing-key-32-bytes-minimum"
class PlaygroundRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = load_dev_corpus_fixture()
        cls.catalog = PlaygroundCatalog(cls.fixture, _SIGNING_KEY)

    def test_server_accepts_only_explicit_loopback_addresses(self) -> None:
        self.assertEqual(require_loopback_host("127.0.0.1"), "127.0.0.1")
        self.assertEqual(require_loopback_host("::1"), "::1")
        for host in ("localhost", "0.0.0.0", "192.0.2.10", ""):
            with self.subTest(host=host):
                with self.assertRaises(ValueError):
                    require_loopback_host(host)

    def test_catalog_exposes_versioned_public_fixture_metadata(self) -> None:
        payload = self.catalog.bootstrap()

        self.assertEqual(payload["schema_version"], "local-playground-bootstrap-v1")
        self.assertEqual(payload["dataset"]["id"], "dev-corpus-v1")
        self.assertEqual(payload["dataset"]["counts"]["active_chunks"], 120)
        self.assertEqual(len(payload["questions"]), 49)
        self.assertEqual(len(payload["personas"]), 7)
        self.assertTrue(payload["capabilities"]["reviewed_questions"])
        self.assertFalse(payload["capabilities"]["answer_generation"])
        self.assertNotIn("answer_retrieval_limits", payload["defaults"])
        self.assertEqual(
            payload["defaults"]["retrieval_limits"],
            asdict(PLAYGROUND_RETRIEVAL_LIMITS),
        )
        self.assertNotIn("principal_id", payload["personas"][0])

    def test_session_token_is_short_lived_and_scoped_to_selected_persona(self) -> None:
        now = int(time.time())
        persona = self.catalog.personas[0]
        session = self.catalog.issue_session(persona.persona_id, now=now)
        claims = jwt.decode(
            session["access_token"],
            _SIGNING_KEY,
            algorithms=["HS256"],
            issuer=PLAYGROUND_ISSUER,
            audience=PLAYGROUND_AUDIENCE,
        )

        self.assertEqual(claims["sub"], persona.principal_id)
        self.assertEqual(claims["tenant_id"], persona.tenant_id)
        self.assertEqual(claims["groups"], list(persona.groups))
        self.assertEqual(claims["scope"], "retrieval:read")
        self.assertEqual(
            claims["exp"] - claims["iat"],
            PLAYGROUND_TOKEN_LIFETIME_SECONDS,
        )
        with self.assertRaises(KeyError):
            self.catalog.issue_session("persona-99", now=now)

    def test_embedder_uses_reviewed_vectors_and_orthogonal_custom_fallback(self) -> None:
        embedder = FixtureQueryEmbedder(self.fixture)
        question = self.fixture.build.questions[0]
        reviewed = embedder.embed(question["query"], tenant_id="tenant-alpha")
        custom = embedder.embed("Northstar revenue", tenant_id="tenant-alpha")
        neutral_index = self.fixture.build.manifest["embedding_profile"]["feature_count"]

        self.assertEqual(reviewed.vector, self.fixture.query_vector(question))
        self.assertEqual(reviewed.embedding_space_id, embedder.embedding_space_id)
        self.assertTrue(embedder.is_reviewed(f"  {question['query']}  "))
        self.assertFalse(embedder.is_reviewed("Northstar revenue"))
        self.assertEqual(sum(value != 0.0 for value in custom.vector), 1)
        self.assertEqual(custom.vector[neutral_index], 1.0)
        self.assertTrue(
            all(
                vector[neutral_index] == 0.0
                for vector in self.fixture.vectors_by_id.values()
            )
        )

    def test_external_embedder_batches_documents_and_binds_query_space(self) -> None:
        calls: list[dict[str, object]] = []

        class Embeddings:
            @staticmethod
            def create(**kwargs: object) -> object:
                calls.append(kwargs)
                texts = kwargs["input"]
                dimensions = int(kwargs["dimensions"])
                return SimpleNamespace(
                    data=[
                        SimpleNamespace(index=index, embedding=[float(index + 1)] * dimensions)
                        for index, _ in enumerate(texts)
                    ]
                )

        client = SimpleNamespace(embeddings=Embeddings())
        embedder = _OpenAICompatibleEmbedder(
            client,
            provider="dashscope-openai-compatible",
            model="text-embedding-v4",
            revision="api-v1",
            dimensions=64,
        )

        vectors = embedder.embed_documents([f"chunk {index}" for index in range(11)])
        query = embedder.embed("question", tenant_id="tenant-alpha")

        self.assertEqual([len(call["input"]) for call in calls], [10, 1, 1])
        self.assertEqual(len(vectors), 11)
        self.assertEqual(len(query.vector), 64)
        self.assertEqual(query.embedding_space_id, embedder.embedding_space_id)
        self.assertTrue(all(call["model"] == "text-embedding-v4" for call in calls))
        self.assertTrue(all(call["dimensions"] == 64 for call in calls))

    def test_routes_serve_self_contained_ui_and_no_store_sessions(self) -> None:
        app = FastAPI()
        attach_playground_routes(app, self.catalog)

        with TestClient(app) as client:
            root = client.get("/", follow_redirects=False)
            page = client.get("/playground")
            bootstrap = client.get("/playground/bootstrap")
            session = client.post(
                "/playground/session",
                json={"persona_id": self.catalog.personas[0].persona_id},
            )
            unknown = client.post(
                "/playground/session",
                json={"persona_id": "persona-99"},
            )

        self.assertEqual(root.status_code, 307)
        self.assertEqual(root.headers["location"], "/playground")
        self.assertEqual(page.status_code, 200)
        self.assertIn("GraphRAG Local Playground", page.text)
        self.assertIn('id="query-input"', page.text)
        self.assertIn("/v1/retrieval", page.text)
        self.assertNotIn("/v1/answers", page.text)
        self.assertNotIn("生成有据回答", page.text)
        self.assertIn("自定义文本使用阿里 Embedding", page.text)
        self.assertNotIn("自定义文本当前只启用 BM25", page.text)
        self.assertIn("ACL experiment", page.text)
        self.assertNotIn("https://", page.text)
        self.assertEqual(page.headers["cache-control"], "no-store")
        self.assertIn("frame-ancestors 'none'", page.headers["content-security-policy"])
        self.assertEqual(bootstrap.status_code, 200)
        self.assertEqual(bootstrap.headers["cache-control"], "no-store")
        self.assertEqual(session.status_code, 200)
        self.assertEqual(session.headers["cache-control"], "no-store")
        self.assertEqual(session.json()["token_type"], "Bearer")
        self.assertEqual(unknown.status_code, 404)


if __name__ == "__main__":
    unittest.main()
