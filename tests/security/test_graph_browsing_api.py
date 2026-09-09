"""Real HTTP/JWT/OperationEnvelope checks for reader graph and source routes."""

from dataclasses import dataclass
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from graphrag_prod.api.backend import GraphRAGApplicationBackend, GraphRAGQueryOperations, QueryEmbedding
from graphrag_prod.api.graph import Neo4jGraphOperations
from graphrag_prod.domain.ids import content_checksum
from graphrag_prod.generation import GroundedGenerationService
from graphrag_prod.graph.browsing import Neo4jPublishedGraphBrowser
from tests.e2e.test_api import _app, _headers, _token
from tests.unit.test_api_backend import RecordingAnswerModel, RecordingDocuments, RecordingEmbedder, RecordingReadiness, RecordingRetrievalEngine, _retrieval_result
from tests.unit.test_graph_browsing import FakeGraphDriver, PIN, SCHEMA
from tests.unit import test_retrieval_subgraph as fixture


@dataclass
class Source:
    document_id: str = "source-document"
    version_id: str = "source-version"
    title: str = "Uploaded observation"
    source_kind: str = "USER_UPLOAD"
    family: str = "canalis-kt"
    asset_keys: tuple[str, ...] = ()
    published_at: object = None
    chunk_count: int = 1
    first_chunk_id: str = "source-chunk"
    canonical_uri: str | None = "https://example.test/source"


@dataclass
class SourcePage:
    sources: tuple[Source, ...]
    has_more: bool = False


@dataclass
class Chunk:
    source: Source
    chunk_id: str
    text: str
    checksum: str
    ordinal: int = 0
    char_start: int = 10
    char_end: int = 0
    page_number: int | None = None
    section: str | None = None
    provenance: object = None
    previous_chunk_id: str | None = None
    next_chunk_id: str | None = None


class SourceCatalog:
    def __init__(self):
        self.calls = []

    def list(self, principal, **kwargs):
        self.calls.append(("list", principal, kwargs))
        return SourcePage((Source(),))

    def chunk(self, principal, *, chunk_id):
        self.calls.append(("chunk", principal, chunk_id))
        if chunk_id != "source-chunk":
            return None
        text = "  Exact source preserves whitespace.\n"
        return Chunk(Source(), chunk_id, text, content_checksum(text), char_end=10 + len(text), provenance={"source_kind": "USER_UPLOAD"})


class GraphBrowsingAPISecurityTests(unittest.TestCase):
    def setUp(self):
        self.driver = FakeGraphDriver()
        self.sources = SourceCatalog()
        self.browser = Neo4jPublishedGraphBrowser(self.driver, cursor_signing_key=b"b" * 32)
        graph = Neo4jGraphOperations(browser=self.browser, source_catalog=self.sources)
        queries = GraphRAGQueryOperations(RecordingRetrievalEngine(_retrieval_result()), RecordingEmbedder(QueryEmbedding((1.0, 0.0), "space-v1")), GroundedGenerationService(RecordingAnswerModel()))
        self.backend = GraphRAGApplicationBackend(documents=RecordingDocuments(), queries=queries, readiness=RecordingReadiness(), graph=graph)
        self.auth = _headers(_token(subject="reader", tenant_id=fixture.TENANT, groups=("asset-engineers",), scope="knowledge:graph:read retrieval:read"))
        self.pin = patch("graphrag_prod.graph.browsing.read_graph_state", return_value=(PIN, SCHEMA))
        self.pin.start()
        self.addCleanup(self.pin.stop)

    def test_http_graph_query_and_evidence_survive_real_mappingproxy_envelope(self):
        with TestClient(_app(self.backend)) as client:
            first = client.post("/v1/knowledge/graph:query", headers=self.auth, json={"page_size": 1})
            self.assertEqual(first.status_code, 200, first.text)
            payload = first.json()
            self.assertIn("schema", payload)
            self.assertNotIn("graph_schema", payload)
            second = client.post("/v1/knowledge/graph:query", headers=self.auth, json={"page_size": 1, "view_token": payload["view_token"], "cursor": payload["page"]["next_cursor"]})
            self.assertEqual(second.status_code, 200, second.text)
            evidence = client.post("/v1/knowledge/graph:evidence", headers=self.auth, json={"view_token": payload["view_token"], "revision_ids": ["owns-published"]})
            self.assertEqual(evidence.status_code, 200, evidence.text)
            item = evidence.json()["items"][0]
            self.assertEqual(item["evidence"]["citation"]["chunk_text"], fixture.CHUNK_TEXT)
            self.assertEqual(item["status"], "PUBLISHED")

    def test_source_only_reader_can_list_and_preserve_exact_chunk_whitespace(self):
        headers = _headers(_token(subject="reader", tenant_id=fixture.TENANT, groups=("asset-engineers",), scope="retrieval:read"))
        with TestClient(_app(self.backend)) as client:
            listed = client.post("/v1/industrial/sources:query", headers=headers, json={})
            self.assertEqual(listed.status_code, 200, listed.text)
            response = client.post("/v1/industrial/sources:chunk", headers=headers, json={"chunk_id": "source-chunk"})
            self.assertEqual(response.status_code, 200, response.text)
            chunk = response.json()["chunk"]
            self.assertTrue(chunk["text"].startswith("  "))
            self.assertTrue(chunk["text"].endswith("\n"))
            self.assertEqual(chunk["checksum"], content_checksum(chunk["text"]))
            missing = client.post("/v1/industrial/sources:chunk", headers=headers, json={"chunk_id": "unknown"})
            self.assertEqual(missing.json(), {"chunk": None})
            forbidden = client.post("/v1/knowledge/graph:query", headers=headers, json={})
            self.assertEqual(forbidden.status_code, 403)
        self.assertTrue(all(principal.tenant_id == fixture.TENANT for _, principal, _ in self.sources.calls))

    def test_new_routes_reject_identity_spoof_and_strict_limits_before_backend(self):
        with TestClient(_app(self.backend)) as client:
            for payload in ({"tenant_id": "victim"}, {"groups": ["secret"]}, {"hops": 3}, {"hops": "2"}, {"page_size": True}, {"seed_entity_ids": ["same", "same"]}):
                with self.subTest(payload=payload):
                    result = client.post("/v1/knowledge/graph:query", headers=self.auth, json=payload)
                    self.assertEqual(result.status_code, 422, result.text)
            result = client.post("/v1/industrial/sources:query", headers=self.auth, json={"access_groups": ["secret"]})
            self.assertEqual(result.status_code, 422)
            result = client.post("/v1/knowledge/graph:query", headers=self.auth, json={"industrial_scope": {}})
            self.assertEqual(result.status_code, 503, result.text)
        self.assertFalse(self.driver.calls)

    def test_changed_cursor_query_and_cross_identity_token_have_fixed_409(self):
        with TestClient(_app(self.backend)) as client:
            response = client.post("/v1/knowledge/graph:query", headers=self.auth, json={"page_size": 1})
            payload = response.json()
            result = client.post("/v1/knowledge/graph:query", headers=self.auth, json={"page_size": 2, "view_token": payload["view_token"], "cursor": payload["page"]["next_cursor"]})
            self.assertEqual(result.status_code, 409, result.text)
            self.assertEqual(result.json()["code"], "graph_view_changed")
            foreign = _headers(_token(subject="other", tenant_id=fixture.TENANT, groups=("asset-engineers",), scope="knowledge:graph:read"))
            result = client.post("/v1/knowledge/graph:evidence", headers=foreign, json={"view_token": payload["view_token"], "revision_ids": ["owns-published"]})
            self.assertEqual(result.status_code, 409, result.text)
            self.assertNotIn("Acme", result.text)

    def test_wrong_groups_receive_empty_view_and_unconfigured_graph_fails_closed(self):
        auth = _headers(_token(subject="limited", tenant_id=fixture.TENANT, groups=("other",), scope="knowledge:graph:read"))
        with TestClient(_app(self.backend)) as client:
            result = client.post("/v1/knowledge/graph:query", headers=auth, json={})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["nodes"], [])
        self.backend._graph = None
        with TestClient(_app(self.backend)) as client:
            result = client.post("/v1/knowledge/graph:query", headers=self.auth, json={})
            self.assertEqual(result.status_code, 503)

    def test_general_source_library_requires_read_scope_and_rejects_identity_injection(self):
        calls=[]
        def read(principal, **kwargs):
            calls.append(principal)
            if kwargs.get('document_id'):
                text='  原文😀\n'
                return dict(document_id='doc',version_id='version',version_number=1,title='资料',source_name='上传',canonical_uri='urn:test:source',chunk_count=1,chunk_id='chunk',ordinal=0,char_start=10,char_end=10+len(text),text=text,checksum=content_checksum(text),page_number=None,section=None)
            return {"items":[],"has_more":False,"next_after":None}
        with patch('graphrag_prod.knowledge.source_library.Neo4jSourceLibrary.read',side_effect=read):
            with TestClient(_app(self.backend)) as client:
                result=client.post('/v1/knowledge/sources:query',headers=self.auth,json={})
                self.assertEqual(result.status_code,200,result.text)
                self.assertEqual(calls[0].tenant_id,fixture.TENANT)
                detail=client.post('/v1/knowledge/sources:read',headers=self.auth,json={'document_id':'doc','version_id':'version'})
                self.assertEqual(detail.status_code,200,detail.text)
                self.assertEqual(detail.json()['text'],'  原文😀\n')
                for body in ({'tenant_id':'victim'},{'limit':101},{'after':True}):
                    self.assertEqual(client.post('/v1/knowledge/sources:query',headers=self.auth,json=body).status_code,422)
                for route,body in (('/v1/knowledge/sources:query',{}),('/v1/knowledge/sources:read',{'document_id':'doc','version_id':'version'})):
                    self.assertEqual(client.post(route,json=body).status_code,401)
                    headers=_headers(_token(scope='ontology:read'))
                    self.assertEqual(client.post(route,headers=headers,json=body).status_code,403)
        self.assertEqual(len(calls),2)


if __name__ == "__main__":
    unittest.main()
