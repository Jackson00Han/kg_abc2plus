"""Executable checks for source-backed demo setup without implicit publication."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from graphrag_prod.playground import PlaygroundCatalog, attach_playground_routes
from graphrag_prod.playground.industrial_demo import get_industrial_demo_kit
from tests.fixtures.dev_corpus import load_dev_corpus_fixture


class PlaygroundDemoUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.kit = get_industrial_demo_kit()
        cls.source = (
            Path(__file__).parents[2]
            / "src/graphrag_prod/playground/static/index.html"
        ).read_text(encoding="utf-8")

    def run_js(self, scenario: str, *, upload: bool = False, detail: bool = False) -> None:
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node is required for executable UI validation")
        code = self.source[
            self.source.index("function demoKit()"):
            self.source.index("function detectedMime(file)")
        ]
        if upload:
            code += self.source[
                self.source.index("async function constructKnowledge()"):
                self.source.index("function reviewEdit(item)")
            ]
        if detail:
            code += self.source[
                self.source.index("async function loadConstructionJob(index, button)"):
                self.source.index("async function constructKnowledge()")
            ]
        harness = r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const fields = new Map();
function $(id) {
  if (!fields.has(id)) fields.set(id, {value: '', textContent: '', disabled: false,
    scrollIntoView() {}, querySelectorAll: () => []});
  return fields.get(id);
}
const kit = input.kit;
const source = kit.files.find(item => item.id === 'authoritative_source');
const state = {bootstrap: {defaults: {industrial_demo: kit}}, identityEpoch: 0,
  constructionBusy: false, demoSourceBinding: null, constructionJobs: []};
const elements = {aboxEditor: {value: 'user draft'}, aboxOutput: {},
  ontologyEditor: {value: ''}, constructionOutput: {textContent: ''}};
const metadata = {canonical_uri: source.metadata.canonical_uri,
  tbox_key: kit.ontology.key, extraction_mode: 'SOURCE_ONLY'};
function result() { return {extraction_mode: 'SOURCE_ONLY', tbox_id: 'real-tbox',
  document_id: 'real-document', version_id: 'real-version', chunks: [{
    chunk_id: 'real-chunk', status: 'SOURCE_ONLY', mention_record_ids: [],
    assertion_record_ids: [], finding_codes: []}]}; }
const requests = [];
const context = vm.createContext({$, fields, kit, source, state, elements, metadata,
  result, assert, requests, Uint8Array, MAX_UPLOAD_BYTES: 5242880,
  escapeHtml: String, shortId: String, showToast() {}, output: (element, value) => {
    element.textContent = typeof value === 'string' ? value : JSON.stringify(value);
  }, currentPersona: () => ({id: 'persona-steward'}),
  selectedDocumentAccessGroups: () => ['alpha-finance'], detectedMime: () => 'text/plain',
  bytesToBase64: () => 'bounded-upload',
  constructionFingerprint: async () => JSON.stringify({content_sha256: source.sha256}),
  nextConstructionOperation: () => 'demo-operation-key', completeConstructionOperation() {},
  loadConstructionJobs: async () => {}, loadReviews: async () => {},
  loadActiveDocuments: async () => {},
  apiRequest: (url, options) => new Promise((resolve, reject) => requests.push({url, options, resolve, reject})),
  flush: () => new Promise(resolve => setImmediate(resolve)),
});
vm.runInContext(input.code, context);
const watchdog = setTimeout(() => {console.error('UI check timed out'); process.exit(1);}, 5000);
vm.runInContext('(async () => {' + input.scenario + '})()', context)
  .then(() => clearTimeout(watchdog))
  .catch(error => {clearTimeout(watchdog); console.error(error); process.exitCode = 1;});
"""
        result = subprocess.run(
            [node, "-e", harness],
            input=json.dumps({"code": code, "kit": self.kit, "scenario": scenario}),
            text=True, capture_output=True, timeout=15, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_demo_downloads_match_committed_checksums_and_reject_unknown_paths(self) -> None:
        app = FastAPI()
        catalog = PlaygroundCatalog(
            load_dev_corpus_fixture(), b"public-demo-test-signing-key-at-least-32-bytes"
        )
        attach_playground_routes(app, catalog)
        with TestClient(app) as client:
            bootstrap = client.get("/playground/bootstrap").json()
            self.assertEqual(bootstrap["defaults"]["industrial_demo"], self.kit)
            for item in self.kit["files"]:
                response = client.get(f'/playground/demo-files/{item["filename"]}')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(hashlib.sha256(response.content).hexdigest(), item["sha256"])
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertIn("attachment", response.headers["content-disposition"])
            for path in (".env", "unknown.txt", "%2E%2E%2F.env"):
                self.assertEqual(client.get(f"/playground/demo-files/{path}").status_code, 404)

    def test_source_binding_uses_real_ids_and_never_imports_or_publishes(self) -> None:
        self.run_js(r"""
assert.equal(bindDemoAuthority(result(), metadata, source.sha256), true);
const draft = JSON.parse(elements.aboxEditor.value);
assert.equal(draft.ontology_version_id, 'real-tbox');
for (const item of [...draft.mentions, ...draft.assertions]) {
  assert.equal(item.evidence.document_id, 'real-document');
  assert.equal(item.evidence.version_id, 'real-version');
  assert.equal(item.evidence.chunk_id, 'real-chunk');
  assert.equal([...source.text].slice(item.evidence.char_start, item.evidence.char_end).join(''), item.evidence.quoted_text);
}
assert.equal(requests.length, 0);
assert.match(elements.aboxOutput.textContent, /尚未|请查看/);
""")

    def test_changed_source_wrong_mode_and_partial_result_never_replace_user_draft(self) -> None:
        self.run_js(r"""
assert.equal(bindDemoAuthority(result(), metadata, 'wrong-hash'), false);
assert.equal(bindDemoAuthority(result(), {...metadata, extraction_mode: 'LLM'}, source.sha256), false);
assert.equal(bindDemoAuthority(result(), {...metadata, canonical_uri: 'urn:other'}, source.sha256), false);
const extracted = result(); extracted.chunks[0].mention_record_ids = ['candidate'];
assert.throws(() => bindDemoAuthority(extracted, metadata, source.sha256));
const missing = result(); missing.document_id = '';
assert.throws(() => bindDemoAuthority(missing, metadata, source.sha256));
assert.equal(elements.aboxEditor.value, 'user draft');
assert.equal(requests.length, 0);
""")

    def test_prefill_selects_mode_but_leaves_file_and_all_writes_to_user(self) -> None:
        self.run_js(r"""
prepareDemoUpload('authoritative_source');
assert.equal($('document-extraction-mode').value, 'SOURCE_ONLY');
assert.equal($('document-uri').value, source.metadata.canonical_uri);
assert.equal($('document-file').value, '');
assert.match($('construct-button').textContent, /不抽取/);
prepareDemoUpload('maintenance_report');
assert.equal($('document-extraction-mode').value, 'LLM');
loadDemoOntology();
assert.equal(JSON.parse(elements.ontologyEditor.value).key, kit.ontology.key);
assert.equal(requests.length, 0);
""")

    def test_identity_switch_clears_generated_tenant_bound_instance_draft(self) -> None:
        self.run_js(r"""
bindDemoAuthority(result(), metadata, source.sha256);
clearDemoSourceBinding();
assert.equal(elements.aboxEditor.value, '');
assert.equal(state.demoSourceBinding, null);
assert.match(elements.aboxOutput.textContent, /身份已切换/);
""")

    def test_upload_captures_source_mode_and_blocks_duplicate_submission(self) -> None:
        self.run_js(r"""
prepareDemoUpload('authoritative_source');
$('document-file').files = [{size: 8, arrayBuffer: async () => new Uint8Array([1]).buffer}];
const first = constructKnowledge();
const duplicate = constructKnowledge();
await flush();
assert.equal(requests.length, 1);
assert.equal(JSON.parse(requests[0].options.body).extraction_mode, 'SOURCE_ONLY');
requests[0].resolve(result());
await Promise.all([first, duplicate]);
assert.equal(JSON.parse(elements.aboxEditor.value).document_id, undefined);
assert.equal(JSON.parse(elements.aboxEditor.value).mentions[0].evidence.document_id, 'real-document');
assert.equal(state.constructionBusy, false);
assert.equal($('construct-button').disabled, false);
""", upload=True)

    def test_identity_change_discards_inflight_upload_result_and_generated_draft(self) -> None:
        self.run_js(r"""
prepareDemoUpload('authoritative_source');
$('document-file').files = [{size: 8, arrayBuffer: async () => new Uint8Array([1]).buffer}];
const pending = constructKnowledge();
await flush();
assert.equal(requests.length, 1);
state.identityEpoch++;
elements.constructionOutput.textContent = 'new identity';
requests[0].resolve(result());
await pending;
assert.equal(elements.aboxEditor.value, 'user draft');
assert.equal(elements.constructionOutput.textContent, 'new identity');
assert.equal(state.demoSourceBinding, null);
""", upload=True)

    def test_upload_shows_both_validation_attempts_without_raw_model_response(self) -> None:
        self.run_js(r"""
prepareDemoUpload('maintenance_report');
$('document-file').files = [{size: 8, arrayBuffer: async () => new Uint8Array([1]).buffer}];
const pending = constructKnowledge();
await flush();
const payload = result();
payload.extraction_mode = 'LLM';
payload.raw_response = 'PRIVATE_MODEL_RESPONSE';
payload.chunks[0].status = 'CANDIDATE';
payload.chunks[0].raw_response = 'PRIVATE_MODEL_RESPONSE';
payload.chunks[0].validation_attempts = [
  {attempt: 1, status: 'REJECTED', finding_codes: ['ENDPOINT_OUTSIDE_EVIDENCE'], response_checksum: 'a'.repeat(64), raw_response: 'PRIVATE_MODEL_RESPONSE'},
  {attempt: 2, status: 'CANDIDATE', finding_codes: [], response_checksum: 'b'.repeat(64)},
];
requests[0].resolve(payload);
await pending;
const summary = JSON.parse(elements.constructionOutput.textContent);
assert.equal(summary.chunks[0].validation_attempts.length, 2);
assert.equal(summary.chunks[0].validation_attempts[0].finding_codes[0], 'ENDPOINT_OUTSIDE_EVIDENCE');
const visible = $('construction-validation-summary').innerHTML;
assert.match(visible, /第 1 次校验：校验未通过/);
assert.match(visible, /ENDPOINT_OUTSIDE_EVIDENCE/);
assert.match(visible, /第二次校验通过/);
assert.match(visible, /人工审核和明确发布/);
assert.ok(!visible.includes('PRIVATE_MODEL_RESPONSE'));
assert.ok(!elements.constructionOutput.textContent.includes('PRIVATE_MODEL_RESPONSE'));
assert.equal(requests.length, 1);
""", upload=True)

    def test_mode_limits_and_legacy_or_source_only_results_remain_explicit(self) -> None:
        self.run_js(r"""
state.bootstrap.capabilities = {construction_limits: {max_validation_attempts: 2, max_llm_chunks: 2, max_chunks: 4}};
prepareDemoUpload('maintenance_report');
assert.match($('construction-mode-note').textContent, /最多 2 Chunks/);
assert.match($('construction-mode-note').textContent, /最多自动纠正一次/);
assert.match($('construction-mode-note').textContent, /超时不自动重试/);
prepareDemoUpload('authoritative_source');
assert.match($('construction-mode-note').textContent, /最多 4 Chunks/);
assert.match($('construction-mode-note').textContent, /不执行 LLM 抽取或自动纠正/);
showConstructionResult(result());
assert.match($('construction-validation-summary').innerHTML, /未执行 LLM 抽取/);
const legacy = result(); legacy.extraction_mode = 'LLM'; legacy.chunks[0].status = 'CANDIDATE';
showConstructionResult(legacy);
assert.match($('construction-validation-summary').innerHTML, /未提供逐次校验摘要/);
assert.ok(!$('construction-validation-summary').innerHTML.includes('第二次校验通过'));
delete state.bootstrap.capabilities;
prepareDemoUpload('maintenance_report');
assert.match($('construction-mode-note').textContent, /当前配置不自动纠正/);
const failed = result(); failed.extraction_mode = 'LLM'; failed.chunks[0].status = 'REJECTED';
failed.chunks[0].validation_attempts = [1, 2].map(attempt => ({attempt, status: 'REJECTED', finding_codes: ['BAD_EVIDENCE'], response_checksum: null}));
showConstructionResult(failed);
assert.match($('construction-validation-summary').innerHTML, /校验仍未通过/);
assert.ok(!$('construction-validation-summary').innerHTML.includes('第二次校验通过'));
""")

    def test_job_details_share_validation_summary_and_discard_old_identity_result(self) -> None:
        self.run_js(r"""
state.constructionJobs = [{job_id: 'job-one'}];
const pending = loadConstructionJob(0, {});
const payload = result(); payload.extraction_mode = 'LLM'; payload.status = 'COMPLETED';
payload.chunks[0].status = 'QUARANTINED';
payload.chunks[0].validation_attempts = [
  {attempt: 1, status: 'REJECTED', finding_codes: ['BAD_EVIDENCE'], response_checksum: 'a'.repeat(64)},
  {attempt: 2, status: 'QUARANTINED', finding_codes: ['UNSUPPORTED_CLAIM'], response_checksum: 'b'.repeat(64)},
];
requests[0].resolve(payload); await pending;
assert.match($('construction-validation-summary').innerHTML, /BAD_EVIDENCE/);
assert.match($('construction-validation-summary').innerHTML, /UNSUPPORTED_CLAIM/);
assert.match($('construction-validation-summary').innerHTML, /结果已隔离/);
assert.equal(JSON.parse(elements.constructionOutput.textContent).status, 'COMPLETED');
const stale = loadConstructionJob(0, {});
state.identityEpoch += 1;
$('construction-validation-summary').innerHTML = 'new identity';
elements.constructionOutput.textContent = 'new identity';
requests[1].resolve(payload); await stale;
assert.equal($('construction-validation-summary').innerHTML, 'new identity');
assert.equal(elements.constructionOutput.textContent, 'new identity');
""", detail=True)


if __name__ == "__main__":
    unittest.main()
