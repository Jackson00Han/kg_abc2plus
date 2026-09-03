"""Unit checks for the local deterministic GraphRAG Playground."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import jwt

from graphrag_prod.api.knowledge_contracts import OntologyImportRequest
from graphrag_prod.domain.ids import embedding_space_id
from graphrag_prod.playground import (
    PLAYGROUND_AUDIENCE,
    PLAYGROUND_ISSUER,
    PLAYGROUND_RETRIEVAL_LIMITS,
    PLAYGROUND_SCOPES,
    PLAYGROUND_TOKEN_LIFETIME_SECONDS,
    FixtureQueryEmbedder,
    PlaygroundCatalog,
    attach_playground_routes,
    require_loopback_host,
)
from tests.fixtures.dev_corpus import load_dev_corpus_fixture
from scripts.run_playground import (
    _OpenAICompatibleEmbedder,
    _PlaygroundKnowledgeOperations,
)


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
        self.assertFalse(payload["capabilities"]["ontology_governance"])
        self.assertEqual(
            payload["defaults"]["industrial_tbox_template"]["key"],
            "industrial-assets",
        )
        OntologyImportRequest.model_validate(
            payload["defaults"]["industrial_tbox_template"]
        )
        self.assertTrue(
            all(
                "llm-candidate" in item["canonical_key_namespaces"]
                for item in payload["defaults"]["industrial_tbox_template"][
                    "entity_types"
                ]
            )
        )
        self.assertNotIn("answer_retrieval_limits", payload["defaults"])
        self.assertEqual(
            payload["defaults"]["retrieval_limits"],
            asdict(PLAYGROUND_RETRIEVAL_LIMITS),
        )
        self.assertNotIn("principal_id", payload["personas"][0])

        governed = PlaygroundCatalog(
            self.fixture,
            _SIGNING_KEY,
            capabilities={
                "ontology_governance": True,
                "document_upload": True,
                "answer_generation": True,
                "extraction_provider": {
                    "protocol": "openai-compatible",
                    "model": "qwen-plus",
                    "purpose": "ontology-constrained extraction only",
                },
            },
        ).bootstrap()
        self.assertEqual(governed["mode"], "retrieval-and-governance")
        self.assertTrue(governed["capabilities"]["document_upload"])
        self.assertFalse(governed["capabilities"]["answer_generation"])
        self.assertNotIn("credential", governed["capabilities"]["extraction_provider"])

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
        self.assertEqual(claims["scope"].split(), list(persona.scopes))
        self.assertIn("retrieval:read", persona.scopes)
        self.assertIn("ontology:read", persona.scopes)
        self.assertEqual(
            claims["exp"] - claims["iat"],
            PLAYGROUND_TOKEN_LIFETIME_SECONDS,
        )
        with self.assertRaises(KeyError):
            self.catalog.issue_session("persona-99", now=now)

    def test_local_personas_preserve_governance_separation_of_duties(self) -> None:
        by_groups = {frozenset(item.groups): item for item in self.catalog.personas}

        self.assertEqual(
            by_groups[frozenset({"alpha-public"})].scopes,
            ("retrieval:read", "ontology:read"),
        )
        self.assertIn(
            "knowledge:construct",
            by_groups[frozenset({"alpha-finance"})].scopes,
        )
        self.assertNotIn(
            "knowledge:review",
            by_groups[frozenset({"alpha-finance"})].scopes,
        )
        self.assertIn(
            "knowledge:review",
            by_groups[frozenset({"alpha-legal"})].scopes,
        )
        self.assertEqual(
            by_groups[frozenset({"alpha-finance", "alpha-legal"})].scopes,
            PLAYGROUND_SCOPES,
        )
        self.assertEqual(
            by_groups[frozenset({"beta-board"})].scopes,
            PLAYGROUND_SCOPES,
        )

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
        uploaded = embedder(chunk=SimpleNamespace(text="uploaded document chunk"))

        self.assertEqual([len(call["input"]) for call in calls], [10, 1, 1, 1])
        self.assertEqual(len(vectors), 11)
        self.assertEqual(len(query.vector), 64)
        self.assertEqual(query.embedding_space_id, embedder.embedding_space_id)
        self.assertEqual(len(uploaded), 64)
        self.assertTrue(all(call["model"] == "text-embedding-v4" for call in calls))
        self.assertTrue(all(call["dimensions"] == 64 for call in calls))

    def test_playground_source_bounds_extraction_provider_calls(self) -> None:
        source = (Path(__file__).parents[2] / "scripts" / "run_playground.py").read_text()

        self.assertIn("with_options(max_retries=0, timeout=30.0)", source)
        self.assertIn('response_format_mode="none"', source)
        self.assertIn("max_output_tokens=2_048", source)
        self.assertIn("max_response_chars=16_384", source)
        self.assertIn("timeout_seconds=30.0", source)
        self.assertIn('"max_chunks": 4', source)
        self.assertIn('"max_model_calls": 4', source)
        self.assertIn('"deadline_seconds": 90.0', source)
        self.assertIn("timeout_seconds=105.0", source)

    def test_construction_refreshes_stale_tenant_embedding_generation(self) -> None:
        driver = Mock()
        driver.execute_query.return_value = (
            [{"corpus_revision": 7}],
            None,
            None,
        )
        active = SimpleNamespace(
            generation_id="generation-1",
            generation_version=1,
            corpus_revision=6,
        )
        target = SimpleNamespace(generation_id="generation-2")
        manager = Mock()
        manager.active_generation.return_value = active
        manager.prepare.return_value = target
        manager.coverage.return_value = SimpleNamespace(complete=True)
        space_id = embedding_space_id(
            "dashscope-openai-compatible",
            "text-embedding-v4",
            "api-v1",
            2,
            "provider-default",
        )
        embedder = SimpleNamespace(
            dimensions=2,
            embedding_space_id=space_id,
            provider="dashscope-openai-compatible",
            model="text-embedding-v4",
            revision="api-v1",
            normalization="provider-default",
        )
        construction = SimpleNamespace(run=lambda *_args, **_kwargs: None)
        operations = _PlaygroundKnowledgeOperations(
            driver=driver,
            database="neo4j",
            construction=construction,
            embedder=embedder,
        )

        with patch(
            "scripts.run_playground.Neo4jEmbeddingIndexManager",
            return_value=manager,
        ):
            operations._refresh_embedding_generation("tenant-alpha")

        manager.prepare.assert_called_once()
        self.assertEqual(manager.prepare.call_args.kwargs["generation_version"], 2)
        manager.activate.assert_called_once_with(
            "generation-2",
            expected_active_generation_id="generation-1",
        )

    def test_construction_replaces_generation_detached_by_ingestion(self) -> None:
        driver = Mock()
        driver.execute_query.side_effect = [
            (
                [
                    {
                        "generation_id": "generation-1",
                        "generation_version": 1,
                        # Even a same-revision generation must be replaced when
                        # ingestion has detached its ACTIVE relationship.
                        "corpus_revision": 7,
                    }
                ],
                None,
                None,
            ),
            ([{"corpus_revision": 7}], None, None),
        ]
        manager = Mock()
        manager.active_generation.return_value = None
        manager.prepare.return_value = SimpleNamespace(generation_id="generation-2")
        manager.coverage.return_value = SimpleNamespace(complete=True)
        space_id = embedding_space_id(
            "dashscope-openai-compatible",
            "text-embedding-v4",
            "api-v1",
            2,
            "provider-default",
        )
        embedder = SimpleNamespace(
            dimensions=2,
            embedding_space_id=space_id,
            provider="dashscope-openai-compatible",
            model="text-embedding-v4",
            revision="api-v1",
            normalization="provider-default",
        )
        operations = _PlaygroundKnowledgeOperations(
            driver=driver,
            database="neo4j",
            construction=SimpleNamespace(run=lambda *_args, **_kwargs: None),
            embedder=embedder,
        )

        with patch(
            "scripts.run_playground.Neo4jEmbeddingIndexManager",
            return_value=manager,
        ):
            operations._refresh_embedding_generation("tenant-alpha")

        self.assertEqual(manager.prepare.call_args.kwargs["generation_version"], 2)
        manager.activate.assert_called_once_with(
            "generation-2",
            expected_active_generation_id=None,
        )

    def test_failed_extraction_still_repairs_advanced_corpus_generation(self) -> None:
        construction = SimpleNamespace(run=lambda *_args, **_kwargs: None)
        operations = _PlaygroundKnowledgeOperations(
            driver=Mock(),
            database="neo4j",
            construction=construction,
            embedder=SimpleNamespace(),
        )
        operations._refresh_embedding_generation = Mock()
        principal = SimpleNamespace(tenant_id="tenant-alpha")

        with patch(
            "scripts.run_playground.Neo4jKnowledgeOperations.construct",
            side_effect=RuntimeError("provider failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "provider failed"):
                operations.construct(principal, object())

        operations._refresh_embedding_generation.assert_called_once_with(
            "tenant-alpha"
        )

    def test_ui_exposes_bounded_retrieval_contract_and_discards_stale_results(
        self,
    ) -> None:
        source = (
            Path(__file__).parents[2]
            / "src"
            / "graphrag_prod"
            / "playground"
            / "static"
            / "index.html"
        ).read_text()

        for field in asdict(PLAYGROUND_RETRIEVAL_LIMITS):
            self.assertIn(f'data-retrieval-limit="{field}"', source)
        for control_id in (
            "filter-document-ids",
            "filter-version-ids",
            "filter-published-before",
            "include-graph",
            "graph-trust-policy",
            "retrieval-defaults-button",
        ):
            self.assertIn(f'id="{control_id}"', source)
        self.assertIn("version_filter: versionFilter()", source)
        self.assertIn("limits: readRetrievalLimits()", source)
        self.assertIn("include_graph: elements.includeGraph.checked", source)
        self.assertIn("graph_trust_policy: elements.graphTrustPolicy.value", source)
        self.assertIn("const requestEpoch = ++state.retrievalEpoch", source)
        self.assertIn("const controller = new AbortController()", source)
        self.assertIn("state.retrievalController?.abort()", source)
        self.assertIn("if (requestEpoch !== state.retrievalEpoch) return", source)
        self.assertIn(
            "elements.persona.addEventListener('change', handleIdentityChange)",
            source,
        )
        self.assertIn("if (identityEpoch !== state.identityEpoch) return", source)
        self.assertIn("activeOntology(item.key)?.tbox_id", source)
        self.assertIn("Candidate cosine ranking", source)
        self.assertNotIn("Candidate rerank", source)

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
        self.assertIn("/v1/knowledge:construct", page.text)
        self.assertIn("/v1/ontologies:import", page.text)
        self.assertIn('id="tab-graph"', page.text)
        self.assertIn('id="governance-workspace"', page.text)
        self.assertIn("literal_semantics", page.text)
        self.assertIn("raw_literal", page.text)
        self.assertNotIn("edit.literal_value", page.text)
        self.assertIn("canonical 语义由服务端", page.text)
        self.assertIn('id="document-access-groups"', page.text)
        self.assertIn("access_groups: accessGroups", page.text)
        self.assertIn("至少选择一个新文档访问组", page.text)
        self.assertIn('id="construction-cost-note"', page.text)
        self.assertIn("item.ontology_version_id", page.text)
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
