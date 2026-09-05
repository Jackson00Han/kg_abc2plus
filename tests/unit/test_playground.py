"""Unit checks for the local deterministic GraphRAG Playground."""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import shutil
import subprocess
import time
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import jwt

from graphrag_prod.api.knowledge_contracts import OntologyImportRequest
from graphrag_prod.api.runtime import DependencyUnavailableError
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
    _Neo4jReadiness,
    _OpenAICompatibleEmbedder,
    _PlaygroundKnowledgeOperations,
    _build_playground_extractor,
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
                "published_graph_quality": True,
                "document_retirement": True,
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
        self.assertTrue(governed["capabilities"]["published_graph_quality"])
        self.assertTrue(governed["capabilities"]["document_retirement"])
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
        self.assertIn(
            "knowledge:quality",
            by_groups[frozenset({"alpha-finance", "alpha-legal"})].scopes,
        )
        self.assertNotIn(
            "knowledge:quality",
            by_groups[frozenset({"alpha-legal"})].scopes,
        )
        self.assertIn(
            "knowledge:lifecycle",
            by_groups[frozenset({"alpha-finance", "alpha-legal"})].scopes,
        )
        self.assertNotIn(
            "knowledge:lifecycle",
            by_groups[frozenset({"alpha-finance"})].scopes,
        )
        self.assertNotIn(
            "knowledge:lifecycle",
            by_groups[frozenset({"alpha-legal"})].scopes,
        )
        self.assertEqual(
            by_groups[frozenset({"beta-board"})].scopes,
            PLAYGROUND_SCOPES,
        )

    @staticmethod
    def _readiness_row(
        tenant_id: str,
        *,
        corpus_revision: int = 7,
        generation_revision: int = 7,
        index_name: str | None = None,
    ) -> dict[str, object]:
        generation_id = f"generation-{tenant_id}"
        return {
            "tenant_id": tenant_id,
            "corpus_revision": corpus_revision,
            "pointers": [
                {
                    "generation_id": generation_id,
                    "index_name": index_name or f"index-{tenant_id}",
                    "state": "ACTIVE",
                    "corpus_revision": generation_revision,
                }
            ],
            "active_ids": [generation_id],
        }

    @staticmethod
    def _readiness(driver: Mock) -> _Neo4jReadiness:
        return _Neo4jReadiness(
            driver,
            "neo4j",
            expected_tenant_ids=("tenant-alpha", "tenant-beta"),
        )

    def test_readiness_requires_current_online_generation_for_every_tenant(self) -> None:
        driver = Mock()
        rows = [
            self._readiness_row("tenant-alpha"),
            self._readiness_row("tenant-beta"),
        ]
        indexes = [
            {"name": "index-tenant-alpha", "type": "VECTOR", "state": "ONLINE"},
            {"name": "index-tenant-beta", "type": "VECTOR", "state": "ONLINE"},
        ]
        driver.execute_query.side_effect = [(rows, None, None), (indexes, None, None)]

        payload = self._readiness(driver).check().payload

        self.assertEqual(payload.status, "ready")
        self.assertEqual(
            payload.checks,
            {
                "neo4j": "ok",
                "embedding_generations": "ok",
                "vector_indexes": "ok",
            },
        )
        self.assertEqual(driver.execute_query.call_count, 2)

    def test_readiness_fails_closed_when_tenant_state_is_missing(self) -> None:
        driver = Mock()
        driver.execute_query.return_value = (
            [self._readiness_row("tenant-alpha")],
            None,
            None,
        )

        payload = self._readiness(driver).check().payload

        self.assertEqual(payload.status, "not_ready")
        self.assertEqual(payload.checks["neo4j"], "ok")
        self.assertEqual(payload.checks["embedding_generations"], "error")
        self.assertEqual(payload.checks["vector_indexes"], "error")
        driver.execute_query.assert_called_once()

    def test_readiness_rejects_multiple_active_pointers(self) -> None:
        driver = Mock()
        alpha = self._readiness_row("tenant-alpha")
        alpha["pointers"] = [
            *alpha["pointers"],
            {
                "generation_id": "generation-tenant-alpha-duplicate",
                "index_name": "index-tenant-alpha-duplicate",
                "state": "ACTIVE",
                "corpus_revision": 7,
            },
        ]
        alpha["active_ids"] = [
            "generation-tenant-alpha",
            "generation-tenant-alpha-duplicate",
        ]
        driver.execute_query.return_value = (
            [alpha, self._readiness_row("tenant-beta")],
            None,
            None,
        )

        payload = self._readiness(driver).check().payload

        self.assertEqual(payload.status, "not_ready")
        self.assertEqual(payload.checks["embedding_generations"], "error")
        driver.execute_query.assert_called_once()

    def test_readiness_rejects_stale_generation_revision(self) -> None:
        driver = Mock()
        driver.execute_query.return_value = (
            [
                self._readiness_row(
                    "tenant-alpha",
                    corpus_revision=8,
                    generation_revision=7,
                ),
                self._readiness_row("tenant-beta"),
            ],
            None,
            None,
        )

        payload = self._readiness(driver).check().payload

        self.assertEqual(payload.status, "not_ready")
        self.assertEqual(payload.checks["embedding_generations"], "error")
        driver.execute_query.assert_called_once()

    def test_readiness_rejects_missing_or_offline_vector_index(self) -> None:
        rows = [
            self._readiness_row("tenant-alpha"),
            self._readiness_row("tenant-beta"),
        ]
        invalid_indexes = (
            [{"name": "index-tenant-alpha", "type": "VECTOR", "state": "ONLINE"}],
            [
                {
                    "name": "index-tenant-alpha",
                    "type": "VECTOR",
                    "state": "ONLINE",
                },
                {
                    "name": "index-tenant-beta",
                    "type": "VECTOR",
                    "state": "POPULATING",
                },
            ],
        )
        for indexes in invalid_indexes:
            with self.subTest(indexes=indexes):
                driver = Mock()
                driver.execute_query.side_effect = [
                    (rows, None, None),
                    (indexes, None, None),
                ]

                payload = self._readiness(driver).check().payload

                self.assertEqual(payload.status, "not_ready")
                self.assertEqual(payload.checks["neo4j"], "ok")
                self.assertEqual(payload.checks["embedding_generations"], "ok")
                self.assertEqual(payload.checks["vector_indexes"], "error")

    def test_readiness_driver_failure_returns_only_safe_checks(self) -> None:
        driver = Mock()
        driver.execute_query.side_effect = RuntimeError(
            "bolt://username:secret@private-host"
        )

        payload = self._readiness(driver).check().payload

        self.assertEqual(payload.status, "not_ready")
        self.assertEqual(set(payload.checks.values()), {"error"})
        self.assertNotIn("private-host", payload.model_dump_json())

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
        from tests.unit.test_construction_extraction import _tbox

        client = Mock()
        extractor = _build_playground_extractor(client, "qwen3.8-max", _tbox())
        client.with_options.assert_called_once_with(max_retries=0, timeout=30.0)
        self.assertIs(extractor.client, client.with_options.return_value)
        self.assertEqual(extractor.model, "qwen3.8-max")
        self.assertFalse(extractor.enable_thinking)
        self.assertTrue(extractor.include_span_hints)
        self.assertEqual(extractor.response_format_mode, "none")
        self.assertEqual(extractor.limits.max_output_tokens, 2048)
        self.assertEqual(extractor.limits.max_response_chars, 16384)
        self.assertEqual(extractor.limits.timeout_seconds, 30.0)
        self.assertIsNone(extractor.seed)
        source = (Path(__file__).parents[2] / "scripts" / "run_playground.py").read_text()
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

    def test_governed_retirement_refreshes_generation_before_returning(self) -> None:
        operations = _PlaygroundKnowledgeOperations(
            driver=Mock(),
            database="neo4j",
            construction=SimpleNamespace(run=lambda *_args, **_kwargs: None),
            embedder=SimpleNamespace(),
        )
        operations._refresh_embedding_generation = Mock()
        principal = SimpleNamespace(tenant_id="tenant-alpha")
        expected = object()

        with patch(
            "scripts.run_playground.Neo4jKnowledgeOperations.retire_document",
            return_value=expected,
        ) as retire:
            actual = operations.retire_document(principal, "document-1", object())

        self.assertIs(actual, expected)
        retire.assert_called_once_with(principal, "document-1", ANY)
        operations._refresh_embedding_generation.assert_called_once_with(
            "tenant-alpha"
        )

        operations._refresh_embedding_generation.side_effect = RuntimeError(
            "bolt://secret-source"
        )
        with patch(
            "scripts.run_playground.Neo4jKnowledgeOperations.retire_document",
            return_value=expected,
        ):
            with self.assertRaises(DependencyUnavailableError) as caught:
                operations.retire_document(principal, "document-1", object())
        self.assertNotIn("secret", caught.exception.public_message)

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
        self.assertIn('data-load-tbox="${index}"', source)
        self.assertIn('data-copy-tbox="${index}"', source)
        self.assertIn('data-download-tbox="${index}"', source)
        self.assertIn("expected_checksum = item.checksum", source)
        self.assertIn("Math.max(0, ...versions) + 1", source)
        self.assertIn("schema: 'graphrag-property-tbox-export-v1'", source)
        self.assertIn("URL.revokeObjectURL(objectUrl)", source)
        self.assertIn("Candidate cosine ranking", source)
        self.assertNotIn("Candidate rerank", source)

    def test_ui_exposes_acl_safe_active_abox_inventory(self) -> None:
        source = (
            Path(__file__).parents[2]
            / "src"
            / "graphrag_prod"
            / "playground"
            / "static"
            / "index.html"
        ).read_text()

        for control_id in (
            "inventory-document-filter",
            "inventory-limit",
            "inventory-refresh-button",
            "inventory-add-removals-button",
            "inventory-summary",
            "inventory-list",
        ):
            self.assertIn(f'id="{control_id}"', source)
        self.assertIn("/v1/knowledge/publication-inventory?${params.toString()}", source)
        self.assertIn("params.set('document_id', documentId)", source)
        self.assertIn("knowledge:quality", source)
        self.assertIn("不能完整查看 active publication 的全部 ACL", source)
        self.assertIn("没有唯一且质量合格的 active publication", source)
        self.assertIn("清单依赖暂不可用", source)
        self.assertIn("data-inventory-select", source)
        self.assertIn("state.selectedInventoryRevisions.has(item.revision_id)", source)
        self.assertIn(".map(item => item.record_id)", source)
        self.assertIn("elements.publicationRemovals.value = recordIds.join('\\n')", source)
        self.assertIn(
            "/v1/knowledge/records/${encodeURIComponent(item.record_id)}/revisions?limit=100",
            source,
        )
        self.assertIn("publication_generation", source)
        self.assertIn("ontology_version_id", source)
        self.assertIn("relationship_properties", source)
        self.assertIn("evidence.char_start", source)
        inventory_source = source[
            source.index("function inventoryLiteralMarkup") : source.index(
                "function renderQuality"
            )
        ]
        self.assertNotIn("quoted_text", inventory_source)
        self.assertNotIn("evidence_text", inventory_source)
        self.assertIn("escapeHtml(item.record_id)", inventory_source)
        self.assertIn("escapeHtml(entity.display_name)", inventory_source)

    def _run_inventory_behavior(self, scenario: str, *, quality: bool = False) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required for executable Playground UI checks")
        source = (
            Path(__file__).parents[2]
            / "src/graphrag_prod/playground/static/index.html"
        ).read_text()
        inventory = source[
            source.index("function inventoryLiteralMarkup") : source.index(
                "function renderQuality"
            )
        ]
        publication = source[
            source.index("async function publishKnowledge") : source.index(
                "async function init"
            )
        ]
        quality_source = source[
            source.index("function renderQuality") : source.index(
                "const documentBlockerLabels"
            )
        ] if quality else ""
        harness = r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const state = {
  identityEpoch: 0, inventoryEpoch: 0, activeInventory: null,
  selectedInventoryRevisions: new Set(), inventoryRevisionHistories: new Map(),
  inventoryHistoryRequests: new Map(), inventoryRemovalRecordIds: new Set(),
  selectedCandidateRevisions: new Set(), publicationCandidates: [],
  approvedRevisions: new Set(), publications: [],
  quality: null, qualityEpoch: 0, qualityHistory: [], qualityHistoryEpoch: 0,
  qualityDetailEpoch: 0, qualitySaveEpoch: 0, qualitySaving: false,
};
const elements = {
  inventoryDocumentFilter: {value: ''},
  inventoryLimit: {value: '100', checkValidity: () => true},
  inventorySummary: {textContent: ''},
  inventoryList: {innerHTML: '', querySelectorAll: () => []},
  publicationRemovals: {value: '', focus() {}},
  publicationRevisions: {value: ''}, publicationOutput: {},
  qualityContent: {innerHTML: ''}, qualitySaveButton: {disabled: false},
  qualitySaveOutput: {textContent: ''}, qualityHistoryPublication: {value: ''},
  qualityHistoryList: {innerHTML: '', querySelectorAll: () => []},
  qualityHistoryDetail: {innerHTML: ''},
};
const requests = [];
function apiRequest(url, options) {
  return new Promise((resolve, reject) => requests.push({url, options, resolve, reject}));
}
function snapshot(id, revision = 'revision-1') {
  return {publication_id: id, publication_generation: 1, manifest_hash: 'digest-' + id,
    ontology_version_id: 'tbox-1', total_record_count: 1, matching_record_count: 1,
    truncated: false, items: [{record_id: 'record-1', revision_id: revision,
      record_kind: 'ENTITY_MENTION', authority_level: 'AUTHORITATIVE',
      entity: {display_name: 'Pump', entity_type: 'Equipment', entity_id: 'entity-1'},
      evidence: {document_id: 'document-1', version_id: 'version-1', chunk_id: 'chunk-1',
        ordinal: 0, char_start: 0, char_end: 4}}]};
}
const context = vm.createContext({state, elements, requests, apiRequest, snapshot,
  flush: () => new Promise(resolve => setImmediate(resolve)),
  URLSearchParams, assert, showToast() {}, escapeHtml: String, number: String,
  shortId: String, output() {}, activePublication: () => state.publications[0],
  loadPublicationCandidates: async () => {}, loadHistory: async () => {},
  loadQuality: async () => {}, loadActiveDocuments: async () => {},
  loadQualityHistory: async () => {},
});
vm.runInContext(input.source, context);
const watchdog = setTimeout(() => { console.error('UI scenario did not finish'); process.exit(1); }, 5000);
vm.runInContext('(async () => {' + input.scenario + '})()', context)
  .then(() => clearTimeout(watchdog))
  .catch(error => { clearTimeout(watchdog); console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            [node, "-e", harness],
            input=json.dumps({"source": inventory + publication + quality_source, "scenario": scenario}),
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_inventory_latest_request_wins_over_stale_success_and_error(self) -> None:
        self._run_inventory_behavior(r"""
const first = loadInventory();
elements.inventoryDocumentFilter.value = 'document-new';
const second = loadInventory();
requests[1].resolve(snapshot('new'));
await second;
requests[0].resolve(snapshot('old'));
await first;
assert.equal(state.activeInventory.publication_id, 'new');
assert.ok(requests[1].url.includes('document_id=document-new'));
const third = loadInventory();
const fourth = loadInventory();
requests[3].resolve(snapshot('newest'));
await fourth;
requests[2].reject({status: 403, message: 'stale denied'});
await third;
assert.equal(state.activeInventory.publication_id, 'newest');
assert.ok(elements.inventorySummary.textContent.includes('newest'));
""")

    def test_inventory_denial_or_identity_change_cannot_restore_old_snapshot(self) -> None:
        self._run_inventory_behavior(r"""
const first = loadInventory();
const second = loadInventory();
requests[1].reject({status: 403, message: 'denied'});
await second;
requests[0].resolve(snapshot('old'));
await first;
assert.equal(state.activeInventory, null);
assert.ok(elements.inventoryList.innerHTML.includes('knowledge:quality'));
const third = loadInventory();
state.identityEpoch += 1;
requests[2].resolve(snapshot('other-identity'));
await third;
assert.equal(state.activeInventory, null);
""")

    def test_inventory_invalid_refresh_clears_derived_removals_and_pending_history(self) -> None:
        self._run_inventory_behavior(r"""
state.activeInventory = snapshot('old');
state.selectedInventoryRevisions.add('revision-1');
elements.publicationRemovals.value = 'manual-record';
addInventoryRemovals();
assert.equal(elements.publicationRemovals.value, 'manual-record\nrecord-1');
const history = loadInventoryRevisionHistory(0, {});
elements.inventoryLimit.checkValidity = () => false;
await loadInventory();
assert.equal(state.activeInventory, null);
assert.equal(state.selectedInventoryRevisions.size, 0);
assert.equal(elements.publicationRemovals.value, 'manual-record');
requests[0].resolve({items: [{revision_id: 'stale'}]});
await history;
assert.equal(state.inventoryRevisionHistories.size, 0);
assert.ok(elements.inventorySummary.textContent.includes('参数无效'));
""")

    def test_inventory_history_discards_older_same_record_responses(self) -> None:
        self._run_inventory_behavior(r"""
state.activeInventory = snapshot('active');
const first = loadInventoryRevisionHistory(0, {});
const second = loadInventoryRevisionHistory(0, {});
requests[1].resolve({items: [{revision_id: 'new-history'}]});
await second;
requests[0].reject({status: 403, message: 'old denial'});
await first;
assert.equal(state.inventoryRevisionHistories.get('record-1').items[0].revision_id, 'new-history');
""")

    def test_publication_mutations_invalidate_inventory_before_request(self) -> None:
        self._run_inventory_behavior(r"""
state.publications = [{publication_id: 'active'}, {publication_id: 'previous'}];
state.activeInventory = snapshot('active');
state.selectedInventoryRevisions.add('revision-1');
addInventoryRemovals();
const publishing = publishKnowledge();
assert.equal(state.activeInventory, null);
assert.equal(state.selectedInventoryRevisions.size, 0);
assert.equal(elements.publicationRemovals.value, '');
assert.equal(JSON.parse(requests[0].options.body).remove_record_ids[0], 'record-1');
requests[0].reject({message: 'conflict'});
await publishing;
state.activeInventory = snapshot('active');
state.selectedInventoryRevisions.add('revision-1');
addInventoryRemovals();
const history = loadInventoryRevisionHistory(0, {});
const rollback = rollbackPublication(1);
assert.equal(state.activeInventory, null);
assert.equal(elements.publicationRemovals.value, '');
requests[2].reject({message: 'conflict'});
await rollback;
requests[1].resolve({items: [{revision_id: 'stale'}]});
await history;
assert.equal(state.inventoryRevisionHistories.size, 0);
""")

    def test_quality_history_reads_preserve_live_report_and_latest_selection(self) -> None:
        self._run_inventory_behavior(r"""
state.quality = {run_id: 'live'};
const first = loadQualityHistory();
elements.qualityHistoryPublication.value = 'publication/2';
const second = loadQualityHistory();
requests[1].resolve({items: [{run_id: 'new'}]});
await second;
requests[0].resolve({items: [{run_id: 'old'}]});
await first;
assert.equal(state.qualityHistory[0].run_id, 'new');
assert.ok(requests[1].url.includes('publication_id=publication%2F2'));
const oldDetail = loadQualityRun('old');
const newDetail = loadQualityRun('new');
requests[3].resolve({report: {run_id: 'new', passed: true}, recorded_by: 'first-expert'});
await newDetail;
requests[2].resolve({report: {run_id: 'old', passed: false}, recorded_by: 'old-expert'});
await oldDetail;
assert.ok(elements.qualityHistoryDetail.innerHTML.includes('first-expert'));
assert.ok(!elements.qualityHistoryDetail.innerHTML.includes('old-expert'));
assert.ok(elements.qualityHistoryDetail.innerHTML.includes('历史观察（非实时）'));
assert.equal(state.quality.run_id, 'live');
assert.ok(requests.every(request => !request.options?.method));
""", quality=True)

    def test_quality_save_is_explicit_single_write_and_shows_first_observer(self) -> None:
        self._run_inventory_behavior(r"""
state.quality = {run_id: 'live'};
const saving = saveQualityRun();
await saveQualityRun();
assert.equal(requests.length, 1);
assert.equal(requests[0].options.method, 'POST');
assert.equal(requests[0].options.body, '{}');
assert.equal(elements.qualitySaveButton.disabled, true);
requests[0].resolve({report: {run_id: 'repeatable-run', passed: false},
  recorded_by: 'original-observer', recorded_at: '2026-01-01T00:00:00Z'});
await flush();
assert.equal(requests[1].url, '/v1/knowledge/quality/runs?limit=10');
requests[1].resolve({items: []});
await saving;
assert.ok(elements.qualitySaveOutput.textContent.includes('original-observer'));
assert.ok(elements.qualitySaveOutput.textContent.includes('2026-01-01T00:00:00Z'));
assert.ok(elements.qualitySaveOutput.textContent.includes('FAIL'));
assert.equal(state.quality.run_id, 'live');
assert.equal(elements.qualitySaveButton.disabled, false);
assert.equal(state.qualitySaving, false);
""", quality=True)

    def test_quality_refresh_and_identity_changes_discard_stale_results(self) -> None:
        self._run_inventory_behavior(r"""
const first = loadQuality();
const second = loadQuality();
requests[1].resolve({run_id: 'new-live', passed: true});
await second;
requests[0].reject({status: 503});
await first;
assert.equal(state.quality.run_id, 'new-live');
const detail = loadQualityRun('old-detail');
const listing = loadQualityHistory();
requests[2].resolve({report: {run_id: 'old-detail'}});
await detail;
assert.ok(!elements.qualityHistoryDetail.innerHTML.includes('old-detail'));
requests[3].resolve({items: []});
await listing;
const saving = saveQualityRun();
state.identityEpoch += 1;
state.qualitySaving = false;
elements.qualitySaveButton.disabled = false;
elements.qualitySaveOutput.textContent = 'new identity';
requests[4].resolve({report: {run_id: 'old identity'}, recorded_by: 'old observer'});
await saving;
assert.equal(elements.qualitySaveOutput.textContent, 'new identity');
assert.equal(requests.length, 5);
assert.equal(elements.qualitySaveButton.disabled, false);
""", quality=True)

    def test_quality_history_errors_are_distinct_and_do_not_reveal_old_detail(self) -> None:
        self._run_inventory_behavior(r"""
for (const [status, expected] of [[403, '全部 ACL'], [404, '未找到'], [409, '一致性冲突'], [503, '暂不可用']]) {
  const loading = loadQualityRun('protected');
  requests[requests.length - 1].reject({status, message: 'raw backend detail'});
  await loading;
  assert.ok(elements.qualityHistoryDetail.innerHTML.includes(expected));
  assert.ok(!elements.qualityHistoryDetail.innerHTML.includes('raw backend detail'));
}
""", quality=True)

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
        self.assertIn("知识库启动基线", page.text)
        self.assertIn("静态启动 fixture，不随上传或撤回变化", page.text)
        self.assertNotIn("知识库状态 <span>LIVE</span>", page.text)
        self.assertIn('id="query-input"', page.text)
        self.assertIn("/v1/retrieval", page.text)
        self.assertIn("/v1/knowledge:construct", page.text)
        self.assertIn("/v1/knowledge/construction-jobs?limit=25", page.text)
        self.assertIn("/v1/knowledge/records/", page.text)
        self.assertIn("/v1/knowledge/publication-candidates?limit=100", page.text)
        self.assertIn('id="construction-job-list"', page.text)
        self.assertIn('id="publication-candidate-list"', page.text)
        self.assertIn('id="quality-content"', page.text)
        self.assertIn('id="quality-refresh-button"', page.text)
        self.assertIn("/v1/knowledge/quality", page.text)
        self.assertIn("当前身份缺少 knowledge:quality", page.text)
        self.assertIn("绝不返回源文本", page.text)
        self.assertIn('id="document-lifecycle-list"', page.text)
        self.assertIn('id="document-lifecycle-summary"', page.text)
        self.assertIn("当前 JWT 完整可见的实时活动数据", page.text)
        self.assertIn("实时授权视图不可用；未用启动 fixture 数字代替", page.text)
        self.assertIn('id="document-lifecycle-refresh-button"', page.text)
        self.assertIn("/v1/knowledge/documents?limit=100", page.text)
        self.assertIn("/v1/knowledge/documents/${encodeURIComponent(item.document_id)}:retire", page.text)
        self.assertIn("graphrag-document-retirement-operation", page.text)
        self.assertIn("expected_active_snapshot_id: item.active_snapshot_id", page.text)
        self.assertIn("source_generation: item.source_generation", page.text)
        self.assertIn("if (!item || item.blocked || (item.blocker_codes || []).length)", page.text)
        self.assertIn("window.confirm", page.text)
        self.assertIn("不会物理删除 source、Chunk", page.text)
        self.assertNotIn("/v1/documents/${encodeURIComponent(item.document_id)}", page.text)
        self.assertIn("graphrag-construction-operation", page.text)
        self.assertIn("constructionFingerprint(bytes", page.text)
        self.assertIn("globalThis.crypto.subtle.digest('SHA-256', bytes)", page.text)
        self.assertIn("operation_key: operationKey", page.text)
        self.assertIn("completeConstructionOperation();", page.text)
        self.assertNotIn("operation_key: `playground-${nonce}`", page.text)
        self.assertIn("requires_replacement", page.text)
        self.assertIn("replace_record_ids: replaceRecordIds", page.text)
        self.assertIn('id="publication-removals"', page.text)
        self.assertIn("remove_record_ids: removeRecordIds", page.text)
        self.assertIn("!revisionIds.length && !removeRecordIds.length", page.text)
        self.assertIn("同一 record 不能同时移除和替换", page.text)
        self.assertIn("不会删除 source", page.text)
        self.assertIn('id="inventory-list"', page.text)
        self.assertIn('id="inventory-summary"', page.text)
        self.assertIn("/v1/knowledge/publication-inventory?", page.text)
        self.assertIn("只返回有界治理元数据和精确证据位置", page.text)
        self.assertIn("对应稳定 record ID 加入第 05 步", page.text)
        self.assertIn("/v1/ontologies:import", page.text)
        self.assertIn('id="tab-graph"', page.text)
        self.assertIn('id="governance-workspace"', page.text)
        self.assertIn("literal_semantics", page.text)
        self.assertIn("raw_literal", page.text)
        self.assertNotIn("edit.literal_value", page.text)
        self.assertIn("canonical 语义由服务端", page.text)
        self.assertIn("/v1/knowledge/entity-resolution/", page.text)
        self.assertIn("/v1/knowledge/entity-resolution:apply", page.text)
        self.assertIn("resolution?.revision !== item.revision", page.text)
        self.assertIn("依赖 assertions，但不会自动批准", page.text)
        self.assertIn("请输入本次实体链接的人工审核依据", page.text)
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
