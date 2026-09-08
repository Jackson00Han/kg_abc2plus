"""Executable checks for the local-only reset confirmation and recovery flow."""

from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess
import unittest


class ResetPageNodes(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.nodes: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if "id" in values:
            self.nodes.append(values)


class PlaygroundResetUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (
            Path(__file__).parents[2]
            / "src/graphrag_prod/playground/static/index.html"
        ).read_text(encoding="utf-8")
        parser = ResetPageNodes()
        parser.feed(cls.source)
        cls.nodes = parser.nodes

    def run_js(self, scenario: str) -> None:
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node is required for executable UI validation")
        code = self.source[
            self.source.index("async function jsonRequest("):
            self.source.index("function parseJsonEditor(")
        ]
        harness = r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const fields = new Map(input.nodes.map(attrs => [attrs.id, {
  hidden: Object.hasOwn(attrs, 'hidden'), disabled: false, textContent: '', open: false,
  showModal() { this.open = true; }, close() { this.open = false; }, focus() { this.focused = true; },
}]));
const $ = id => { assert.ok(fields.has(id), 'Unknown page control: ' + id); return fields.get(id); };
const state = {localReset: null};
const values = new Map();
const sessionStorage = {getItem: key => values.get(key) ?? null,
  setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key),
  key: index => [...values.keys()][index] ?? null, get length() { return values.size; }};
const requests = [], timers = [];
const config = {enabled: true, control_token: 'local-test-control', generation: 'generation-before', state: 'READY', reset_id: null, error_code: null};
const context = vm.createContext({$, fields, state, assert, values, sessionStorage, requests, config,
  window: {setTimeout: (callback, delay) => {timers.push({callback, delay}); return timers.length;}, clearTimeout() {}},
  location: {reload: () => {context.reloads += 1;}}, reloads: 0,
  crypto: {randomUUID: () => `00000000-0000-4000-8000-${String(++context.nonce).padStart(12, '0')}`}, nonce: 0,
  currentPersona: () => ({id: 'alpha-finance', tenant_id: 'alpha'}),
  fetch: (url, options) => new Promise((resolve, reject) => requests.push({url, options, reject,
    reply: (payload, status=200) => resolve({ok: status >= 200 && status < 300, status, json: async () => payload})})),
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
            input=json.dumps({"code": code, "nodes": self.nodes, "scenario": scenario}),
            text=True, capture_output=True, timeout=15, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_one_optional_reset_button_names_complete_scope(self) -> None:
        ids = [node["id"] for node in self.nodes]
        self.assertEqual(ids.count("local-reset-button"), 1)
        self.assertLess(ids.index("local-reset-button"), ids.index("step-ontology"))
        self.assertIn("所有身份的本体、权威实例、上传文档", self.source)
        self.assertIn("${counts.documents} 份文档、${counts.active_chunks} 个 Chunk", self.source)
        self.assertIn("一键重置演示", self.source)
        self.assertIn("已下载到电脑的文件不受影响", self.source)
        self.run_js(r"""
initializeLocalReset(undefined);
initializeLocalReset({enabled: false});
assert.equal(state.localReset, null);
assert.equal($('local-reset-button').hidden, true);
assert.equal(requests.length, 0);
""")

    def test_cancel_confirmation_does_not_submit_or_clear_existing_state(self) -> None:
        self.run_js(r"""
initializeLocalReset(config);
values.set('graphrag-construction-operation', 'existing-upload');
openLocalReset();
assert.equal($('local-reset-dialog').open, true);
await assert.rejects(apiRequest('/v1/ontologies:import', {method:'POST'}), /重置操作/);
cancelLocalReset();
assert.equal($('local-reset-dialog').open, false);
assert.equal(state.localReset.blocked, false);
assert.equal(values.get('graphrag-construction-operation'), 'existing-upload');
assert.equal(requests.length, 0);
""")

    def test_accepted_reset_is_single_submission_and_success_clears_only_related_state(self) -> None:
        self.run_js(r"""
initializeLocalReset(config);
values.set('graphrag-construction-operation', 'old');
values.set('graphrag-document-retirement-operation:alpha:doc', 'old');
values.set('unrelated-setting', 'keep');
openLocalReset();
const pending = confirmLocalReset();
await confirmLocalReset();
assert.equal(requests.length, 1);
assert.equal(requests[0].options.method, 'POST');
assert.equal(requests[0].options.headers['X-Playground-Reset-Token'], config.control_token);
const body = JSON.parse(requests[0].options.body);
assert.equal(body.confirmation, 'RESET_ALL');
assert.equal(body.expected_generation, config.generation);
requests[0].reply({...config, state:'RUNNING', generation:'generation-after', reset_id:body.operation_id}, 202);
await pending;
assert.equal(state.localReset.blocked, true);
assert.equal(state.localReset.pageGeneration, config.generation);
cancelLocalReset();
assert.equal($('local-reset-dialog').open, true);
const checking = checkLocalResetStatus();
requests[1].reply({...config, state:'SUCCEEDED', generation:'generation-after', reset_id:body.operation_id});
await checking;
assert.equal(reloads, 1);
assert.equal(values.has('graphrag-construction-operation'), false);
assert.equal(values.has('graphrag-document-retirement-operation:alpha:doc'), false);
assert.equal(values.has(RESET_OPERATION_KEY), false);
assert.equal(values.get('unrelated-setting'), 'keep');
assert.equal(JSON.parse(values.get(RESET_RETURN_KEY)).persona_id, 'alpha-finance');
""")

    def test_uncertain_submission_checks_status_without_repeating_post(self) -> None:
        self.run_js(r"""
initializeLocalReset(config);
openLocalReset();
const pending = confirmLocalReset();
const body = JSON.parse(requests[0].options.body);
requests[0].reject(new Error('response lost'));
await flush();
assert.equal(requests.length, 2);
assert.equal(requests[1].options.method, undefined);
requests[1].reply({...config, state:'RUNNING', generation:'new-generation', reset_id:body.operation_id});
await pending;
assert.equal(state.localReset.phase, 'running');
await confirmLocalReset();
assert.equal(requests.filter(item => item.options.method === 'POST').length, 1);
""")

    def test_failed_reset_blocks_business_until_a_new_explicit_confirmation(self) -> None:
        self.run_js(r"""
const failed = {...config, state:'FAILED', reset_id:'failed-operation', error_code:'RESTORE_FAILED'};
initializeLocalReset(failed);
assert.equal(state.localReset.phase, 'failed');
await assert.rejects(apiRequest('/v1/knowledge:construct', {method:'POST'}), /重置操作/);
await confirmLocalReset();
assert.equal(state.localReset.phase, 'confirm');
cancelLocalReset();
assert.equal(state.localReset.blocked, true);
assert.equal(requests.length, 0);
const retry = confirmLocalReset();
const body = JSON.parse(requests[0].options.body);
assert.notEqual(body.operation_id, failed.reset_id);
requests[0].reply({...config, state:'SUCCEEDED', generation:'new-generation', reset_id:body.operation_id}, 202);
await retry;
assert.equal(reloads, 1);
""")

    def test_stale_page_sends_its_generation_and_cannot_submit_again(self) -> None:
        self.run_js(r"""
initializeLocalReset(config);
const request = apiRequest('/v1/ontologies:import', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
requests[0].reply({access_token:'test-access'});
await flush();
assert.equal(requests[1].options.headers['X-Playground-Generation'], config.generation);
requests[1].reply({error:{code:'PLAYGROUND_RESET_STALE', message:'stale'}}, 409);
await assert.rejects(request, /页面提示刷新/);
assert.equal(state.localReset.phase, 'stale');
await assert.rejects(apiRequest('/v1/ontologies:import', {method:'POST'}), /重置操作/);
requests[2].reply({...config, state:'SUCCEEDED', generation:'other-generation', reset_id:'other-operation'});
await flush();
assert.equal(state.localReset.phase, 'stale');
assert.equal($('local-reset-reload').hidden, false);
assert.equal(reloads, 0);
""")

    def test_refresh_recovers_running_reset_and_busy_rejection_can_be_cancelled(self) -> None:
        self.run_js(r"""
values.set(RESET_OPERATION_KEY, JSON.stringify({operation_id:'saved-operation', expected_generation:'old-generation'}));
initializeLocalReset({...config, state:'RUNNING', reset_id:'saved-operation'});
assert.equal(state.localReset.phase, 'running');
assert.equal(requests.length, 0);
const checking = checkLocalResetStatus();
requests[0].reply({...config, state:'SUCCEEDED', reset_id:'saved-operation'});
await checking;
assert.equal(reloads, 1);
state.localReset = null;
initializeLocalReset(config);
openLocalReset();
const pending = confirmLocalReset();
requests[1].reply({error:{code:'PLAYGROUND_RESET_BUSY', message:'busy'}}, 409);
await flush();
requests[2].reply(config);
await pending;
assert.equal(state.localReset.phase, 'not-started');
assert.equal(values.has(RESET_OPERATION_KEY), false);
cancelLocalReset();
assert.equal(state.localReset.blocked, false);
assert.equal(requests.filter(item => item.options.method === 'POST').length, 1);
""")
