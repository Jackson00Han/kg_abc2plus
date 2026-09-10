"""HTTP industrial context reaches the governed workflow without added privileges."""
from dataclasses import replace
import base64
import unittest
from unittest.mock import Mock

from fastapi.testclient import TestClient
from graphrag_prod.api import GraphRAGApplicationBackend, JWTAuthConfig, JWTAuthenticator, create_app
from graphrag_prod.api.knowledge import Neo4jKnowledgeOperations
from graphrag_prod.industrial.construction import INDUSTRIAL_TENANT, INDUSTRIAL_TBOX_KEY, IndustrialUploadContext, Neo4jIndustrialUploadPolicy, industrial_upload_parser
from graphrag_prod.construction import ConstructionAuthorizationError
from tests.security.test_knowledge_api_security import ISSUER, AUDIENCE, SECRET, _headers
from tests.unit.test_construction_workflow import _tbox, _Extractor, _workflow


class IndustrialUploadSecurityTests(unittest.TestCase):
    def setUp(self):
        tbox = replace(_tbox(INDUSTRIAL_TENANT), key=INDUSTRIAL_TBOX_KEY)
        self.workflow, self.audit, _, self.pipeline = _workflow(extractor=_Extractor(tbox))
        self.policy = Neo4jIndustrialUploadPolicy(object())
        self.policy.resolver = Mock()
        self.policy.persist = Mock()
        self.workflow.industrial_upload_policy = self.policy
        self.workflow.industrial_parser = industrial_upload_parser()
        knowledge = Neo4jKnowledgeOperations(driver=Mock(), construction=self.workflow)
        backend = GraphRAGApplicationBackend(documents=Mock(), queries=Mock(), readiness=Mock(), knowledge=knowledge)
        app = create_app(authenticator=JWTAuthenticator(JWTAuthConfig(issuer=ISSUER, audience=AUDIENCE, secret=SECRET)), backend=backend)
        self.context = TestClient(app)
        self.client = self.context.__enter__()
        self.headers = _headers(tenant_id=INDUSTRIAL_TENANT, groups=("maintenance",), scope="knowledge:construct")
        self.body = {"operation_key": "industrial-http-upload-0001", "canonical_uri": f"industrial-upload://{INDUSTRIAL_TENANT}/report-1",
            "title": "Inspection report", "source_name": "User upload", "mime_type": "text/plain", "language": "en",
            "tbox_key": INDUSTRIAL_TBOX_KEY, "access_groups": ["maintenance"], "extraction_mode": "SOURCE_ONLY",
            "industrial_context": {"family": "canalis-kt", "asset_keys": []},
            "content_base64": base64.b64encode(b"Inspection result remains unconfirmed.").decode()}

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def test_http_context_and_jwt_actor_reach_source_job_without_import_capability(self):
        response = self.client.post("/v1/knowledge:construct", headers=self.headers, json=self.body)
        self.assertEqual(response.status_code, 200, response.text)
        principal, metadata, parsed, job = self.policy.persist.call_args.args
        self.assertEqual(principal.tenant_id, INDUSTRIAL_TENANT)
        self.assertNotIn("knowledge:import", principal.capabilities)
        self.assertEqual(metadata.industrial_context, IndustrialUploadContext("canalis-kt"))
        self.assertIn('"source_kind":"USER_UPLOAD"', job.industrial_context_json)
        self.assertEqual(response.json()["extraction_mode"], "SOURCE_ONLY")

    def test_caller_cannot_forge_source_kind_actor_or_tenant_inside_context(self):
        for field, value in (("source_kind", "OFFICIAL_PUBLICATION"), ("tenant_id", "tenant-alpha"),
                             ("principal_id", "administrator"), ("authority", "AUTHORITATIVE")):
            body = {**self.body, "industrial_context": {**self.body["industrial_context"], field: value}}
            response = self.client.post("/v1/knowledge:construct", headers=self.headers, json=body)
            self.assertEqual(response.status_code, 422, response.text)
        self.assertFalse(self.pipeline.requests)
        self.policy.persist.assert_not_called()

    def test_missing_context_reserved_seed_uri_and_wrong_tbox_fail_before_ingestion(self):
        variants = ({**self.body, "industrial_context": None},
                    {**self.body, "canonical_uri": f"industrial://{INDUSTRIAL_TENANT}/seed"},
                    {**self.body, "tbox_key": "industrial-assets"})
        for body in variants:
            response = self.client.post("/v1/knowledge:construct", headers=self.headers, json=body)
            self.assertEqual(response.status_code, 409, response.text)
        self.assertFalse(self.pipeline.requests)
        self.assertFalse(self.audit.jobs)

    def test_generic_industrial_tenant_document_keeps_jwt_acl_and_no_product_provenance(self):
        body = {**self.body, "canonical_uri": "urn:local:controlled-upload:general-report", "industrial_context": None}
        response = self.client.post("/v1/knowledge:construct", headers=self.headers, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        job = self.audit.jobs[response.json()["job_id"]]
        self.assertEqual(job.tenant_id, INDUSTRIAL_TENANT)
        self.assertEqual(job.access_groups, frozenset({"maintenance"}))
        self.assertIsNone(job.industrial_context_json)
        self.policy.persist.assert_not_called()
        self.policy.resolver.resolve.assert_not_called()

    def test_generic_documents_cannot_expand_acl_or_promote_authority_without_import(self):
        body = {**self.body, "canonical_uri": "urn:local:controlled-upload:generic-protected", "industrial_context": None}
        for changed in ({**body, "access_groups": ["engineering"]},
                        {**body, "knowledge_scope": "AUTHORITATIVE", "extraction_mode": "LLM"}):
            with self.subTest(changed=changed):
                response = self.client.post("/v1/knowledge:construct", headers=self.headers, json=changed)
                self.assertEqual(response.status_code, 403, response.text)
        self.assertFalse(self.pipeline.requests)
        self.assertFalse(self.audit.jobs)

    def test_rendered_utf8_budget_returns_422_before_any_source_write(self):
        body = {**self.body, "title": "题" * 512,
                "content_base64": base64.b64encode(("测" * 900).encode()).decode()}
        response = self.client.post("/v1/knowledge:construct", headers=self.headers, json=body)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["code"], "construction_input_limit")
        self.assertIn("UTF-8 byte budget", response.text)
        self.assertFalse(self.pipeline.requests)
        self.assertFalse(self.audit.jobs)
        self.policy.persist.assert_not_called()

    def test_blank_padding_chunk_is_a_422_input_error_before_writes(self):
        source = " " * 901 + "Inspection remains unconfirmed."
        parsed = self.workflow.industrial_parser.parse(source.encode(), mime_type="text/plain")
        self.assertEqual("".join(chunk.text for chunk in parsed.chunks), source)
        self.assertFalse(parsed.chunks[0].text.strip())
        body = {**self.body, "content_base64": base64.b64encode(source.encode()).decode()}
        response = self.client.post("/v1/knowledge:construct", headers=self.headers, json=body)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["code"], "invalid_request")
        self.assertFalse(self.pipeline.requests)
        self.assertFalse(self.audit.jobs)
        self.policy.persist.assert_not_called()


if __name__ == "__main__":
    unittest.main()
