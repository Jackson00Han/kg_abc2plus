"""Upload decisions are enforced before providers and bind the pump source view."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest

from fastapi.testclient import TestClient

from graphrag_prod.api import GraphRAGApplicationBackend, JWTAuthConfig, JWTAuthenticator, create_app
from graphrag_prod.api.knowledge import Neo4jKnowledgeOperations
from graphrag_prod.api.knowledge_contracts import KnowledgeConstructionRequest
from graphrag_prod.api.runtime import (AuthorizationError, DependencyUnavailableError,
    OperationKind, UploadInProgressError, UploadReviewRequiredError)
from graphrag_prod.construction.upload_guard import UploadAlreadyRunning
from graphrag_prod.domain import Principal
from tests.e2e.test_knowledge_api import _Documents, _Queries, _Readiness, _headers, ISSUER, AUDIENCE, SECRET

PUMP = Path('src/graphrag_prod/playground/static/industrial-demo-v1/authoritative_source.txt').read_bytes()


def request(**changes):
    payload = dict(operation_key='pump-upload-test-0001', canonical_uri='urn:pump:test:upload',
        title='循环水泵资料', source_name='循环水泵测试包', mime_type='text/plain',
        tbox_key='pump-maintenance-demo', access_groups=['engineers'], extraction_mode='SOURCE_ONLY',
        content_base64=base64.b64encode(PUMP).decode())
    return KnowledgeConstructionRequest.model_validate({**payload, **changes})


class Check:
    def __init__(self):
        self.calls = 0
        self.report = dict(checksum='a'*64, original_checksum='b'*64, exact_matches=[],
            similar_matches=[], similarity_checked=True, truncated=False, truncation_reasons=[],
            compared_versions=0, method='character-5-shingle-jaccard-v1', threshold=0.85)
        self.error = None

    def check(self, *args, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return deepcopy(self.report)

    def duplicate(self, kind='EXACT'):
        self.report['exact_matches' if kind == 'EXACT' else 'similar_matches'] = [dict(
            document_id='pump-source', version_id='pump-version', title='循环水泵',
            canonical_uri='urn:pump:test:existing', match_kind=kind, similarity=1.0 if kind == 'EXACT' else .92,
            difference=None)]


class Guard:
    active = False
    calls = 0
    busy = False

    @contextmanager
    def hold(self, *args):
        self.calls += 1
        if self.busy:
            raise UploadAlreadyRunning()
        self.active = True
        try:
            yield
        finally:
            self.active = False


class Construction:
    def __init__(self, guard):
        self.calls = 0
        self.guard = guard

    def run(self, principal, content, metadata):
        assert self.guard.active
        self.calls += 1
        return SimpleNamespace(job_id='pump-job', extraction_mode='SOURCE_ONLY', document_id='pump-source',
            version_id='pump-version', snapshot_id='pump-snapshot', tbox_id='pump-tbox', chunks=(SimpleNamespace(
                chunk_id='pump-chunk', artifact_id='pump-artifact', status='SOURCE_ONLY', finding_codes=(),
                mention_record_ids=(), assertion_record_ids=(), replayed=False),))


class UploadAPITests(unittest.TestCase):
    def setUp(self):
        self.check, self.guard = Check(), Guard()
        self.construction = Construction(self.guard)
        self.api = Neo4jKnowledgeOperations(driver=object(), construction=self.construction,
            upload_preflight=self.check, upload_guard=self.guard)
        self.principal = Principal('pump-user', 'pump-workspace', frozenset({'engineers'}),
            frozenset({'knowledge:construct'}))

    def test_readonly_preflight_does_not_reserve_or_construct(self):
        result = self.api.upload_preflight(self.principal, request()).payload
        self.assertEqual(len(result.review_token), 64)
        self.assertEqual(self.guard.calls, 0)
        self.assertEqual(self.construction.calls, 0)
        self.assertFalse(OperationKind.KNOWLEDGE_PREFLIGHT.is_write)
        self.assertTrue(OperationKind.KNOWLEDGE_PREFLIGHT.is_retry_safe)

    def test_no_duplicate_constructs_under_guard(self):
        self.api.construct(self.principal, request())
        self.assertEqual(self.construction.calls, 1)
        self.assertFalse(self.guard.active)

    def test_duplicate_similar_and_incomplete_checks_require_decision(self):
        for scenario in ('EXACT', 'SIMILAR', 'LIMIT'):
            with self.subTest(scenario=scenario):
                self.check = Check()
                self.api.preflight = self.check
                if scenario == 'LIMIT':
                    self.check.report.update(truncated=True, truncation_reasons=['SIMILARITY_CANDIDATE_LIMIT'])
                else:
                    self.check.duplicate(scenario)
                with self.assertRaises(UploadReviewRequiredError):
                    self.api.construct(self.principal, request())
                self.assertFalse(self.guard.active)
        self.assertEqual(self.construction.calls, 0)

    def test_explicit_decision_runs_only_with_same_source_view_and_settings(self):
        self.check.duplicate()
        token = self.api.upload_preflight(self.principal, request()).payload.review_token
        self.api.construct(self.principal, request(preflight_token=token))
        self.assertEqual(self.construction.calls, 1)
        for changed in (dict(title='循环水泵新标题'), dict(content_base64=base64.b64encode(PUMP+b'\nBC-P-202').decode())):
            # The real preflight checksum changes with content; simulate its output here.
            if 'content_base64' in changed:
                self.check.report['checksum'] = 'c' * 64
            with self.subTest(changed=list(changed)), self.assertRaises(UploadReviewRequiredError):
                self.api.construct(self.principal, request(preflight_token=token, **changed))
        self.check.report['exact_matches'][0]['version_id'] = 'pump-new-version'
        with self.assertRaises(UploadReviewRequiredError):
            self.api.construct(self.principal, request(preflight_token=token))
        self.assertEqual(self.construction.calls, 1)

    def test_unavailable_check_and_concurrent_upload_never_call_provider(self):
        self.check.error = RuntimeError('private source failure')
        with self.assertRaises(DependencyUnavailableError):
            self.api.construct(self.principal, request())
        self.assertFalse(self.guard.active)
        self.guard.busy = True
        calls = self.check.calls
        with self.assertRaises(UploadInProgressError):
            self.api.construct(self.principal, request())
        self.assertEqual(self.check.calls, calls)
        self.assertEqual(self.construction.calls, 0)

    def test_acl_and_authority_checked_before_preflight_and_reservation(self):
        for changes in (dict(access_groups=['restricted']), dict(knowledge_scope='AUTHORITATIVE')):
            for method in (self.api.upload_preflight, self.api.construct):
                with self.assertRaises(AuthorizationError):
                    method(self.principal, request(**changes))
        self.assertEqual(self.check.calls, 0)
        self.assertEqual(self.guard.calls, 0)

    def test_http_read_and_direct_construct_share_authorization_and_guard(self):
        self.check.duplicate()
        backend = GraphRAGApplicationBackend(documents=_Documents(), queries=_Queries(),
            readiness=_Readiness(), knowledge=self.api)
        app = create_app(authenticator=JWTAuthenticator(JWTAuthConfig(issuer=ISSUER, audience=AUDIENCE, secret=SECRET)), backend=backend)
        body = request().model_dump(mode='json')
        with TestClient(app) as client:
            unauthorized = client.post('/v1/knowledge:preflight', json=body)
            self.assertEqual(unauthorized.status_code, 401)
            response = client.post('/v1/knowledge:preflight', headers=_headers(), json=body)
            self.assertEqual(response.status_code, 200, response.text)
            rejected = client.post('/v1/knowledge:construct', headers=_headers(), json=body)
            self.assertEqual(rejected.status_code, 409, rejected.text)
            self.assertIn('upload_review_required', rejected.text)
            self.assertEqual(self.construction.calls, 0)
            accepted = client.post('/v1/knowledge:construct', headers=_headers(), json={**body, 'preflight_token':response.json()['review_token']})
            self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(self.construction.calls, 1)


if __name__ == '__main__':
    unittest.main()
