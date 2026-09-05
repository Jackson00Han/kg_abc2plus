"""Executable checks for read-only automatic review-queue entity matching."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest


class PlaygroundResolutionTests(unittest.TestCase):
    def run_ui(self, scenario: str) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required for executable Playground UI checks")
        page = (
            Path(__file__).parents[2]
            / "src/graphrag_prod/playground/static/index.html"
        ).read_text()
        source = page[
            page.index("function reviewEdit(") : page.index("function chosenReviews(")
        ]
        source += page[
            page.index("async function publishOntology(") : page.index("async function importABox(")
        ]
        source += page[
            page.index("async function publishKnowledge(") : page.index("async function init(")
        ]
        harness = r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const state = {identityEpoch: 0, reviewEpoch: 0, reviews: [], resolutions: new Map(),
  resolutionQueue: [], resolutionActive: 0, revisionHistories: new Map(),
  approvedRevisions: new Set(), selectedCandidateRevisions: new Set(),
  publicationCandidates: [], publications: [{publication_id: 'active'}, {publication_id: 'old'}],
  ontologies: [{tbox_id: 'tbox', key: 'pump'}]};
const panels = new Map();
const draft = {editor: '', selected: false, editEnabled: false, focused: false};
const reviewList = {
  html: '', writes: 0,
  set innerHTML(value) {
    this.html = value;
    this.writes += 1;
    panels.clear();
    Object.assign(draft, {editor: '', selected: false, editEnabled: false, focused: false});
    for (const match of value.matchAll(/data-resolution-panel="(\d+)"/g)) {
      panels.set(match[1], {innerHTML: '', querySelectorAll: () => []});
    }
  },
  get innerHTML() { return this.html; },
  querySelectorAll() { return []; },
  querySelector(selector) {
    const match = selector.match(/data-resolution-panel="(\d+)"/);
    return match ? panels.get(match[1]) : null;
  },
};
const elements = {reviewList, publicationRevisions: {value: ''},
  publicationRemovals: {value: ''}, publicationOutput: {textContent: ''}};
const requests = [];
let promptCount = 0;
let confirmCount = 0;
function apiRequest(url, options) {
  return new Promise((resolve, reject) => requests.push({url, options, settled: false,
    resolve(value) { this.settled = true; resolve(value); },
    reject(error) { this.settled = true; reject(error); }}));
}
function item(id, revision = 1, kind = 'ENTITY_MENTION') {
  return {record_id: id, revision, revision_id: id + '-' + revision, record_kind: kind,
    entity: {canonical_name: id, entity_type: 'Equipment', canonical_key: 'equipment:' + id},
    ...(kind === 'ENTITY_MENTION' ? {} : {subject: {canonical_name: 'subject'},
      object_entity: {canonical_name: 'object'}}),
    trust: {}, confidence: 1, evidence: {chunk_id: 'chunk-' + id}};
}
function resolution(id, revision = 1, outcome = 'AUTO_LINK') {
  return {record_id: id, revision, identity_properties: [], suggestions: [{outcome,
    reason: 'unique exact identifier', confidence: 1, rule_version: 'rules:v1',
    matcher_version: 'matcher:v1',
    target: outcome === 'NO_MATCH' || outcome === 'CONFLICT' ? null : {
      entity_id: 'authority-' + id, canonical_name: 'Authority ' + id}, evidence: []}]};
}
const context = vm.createContext({state, elements, requests, panels, draft, item, resolution,
  assert, apiRequest, flush: () => new Promise(resolve => setImmediate(resolve)),
  showToast() {}, escapeHtml: String, shortId: String,
  relationshipPropertiesMarkup: () => '', literalSemanticsMarkup: () => '',
  prompt: () => { promptCount += 1; return null; },
  confirm: () => { confirmCount += 1; return false; },
  confirmations: () => ({promptCount, confirmCount}), loadActiveDocuments: async () => {},
  activeOntology: () => state.ontologies[0], activePublication: () => state.publications[0],
  loadOntologies: async () => {}, invalidateInventory() {}, loadPublicationCandidates: async () => {},
  loadHistory: async () => {}, loadInventory: async () => {}, loadQuality: async () => {},
  loadQualityHistory: async () => {},
  output: (element, value) => { element.textContent = typeof value === 'string' ? value : JSON.stringify(value); },
});
vm.runInContext(input.source, context);
const watchdog = setTimeout(() => { console.error('UI scenario did not finish'); process.exit(1); }, 5000);
vm.runInContext('(async () => {' + input.scenario + '})()', context)
  .then(() => clearTimeout(watchdog))
  .catch(error => { clearTimeout(watchdog); console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            [node, "-e", harness],
            input=json.dumps({"source": source, "scenario": scenario}),
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_loading_queue_matches_entities_with_two_read_only_workers(self) -> None:
        self.run_ui(r"""
const loading = loadReviews();
requests[0].resolve({items: [item('a'), item('b'), item('c'), item('d'), item('e'),
  item('fact', 1, 'RELATIONSHIP_ASSERTION')]});
await loading;
assert.equal(state.resolutionActive, 2);
assert.equal(requests.length, 3);
for (let index = 1; index <= 5; index += 1) {
  const request = requests[index];
  const id = decodeURIComponent(request.url.split('/').pop().split('?')[0]);
  request.resolve(resolution(id));
  await flush();
  assert.ok(state.resolutionActive <= 2);
  assert.ok(requests.filter(value => !value.settled).length <= 2);
}
assert.equal(state.resolutionActive, 0);
assert.equal(state.resolutions.size, 5);
assert.ok([...state.resolutions.values()].every(value => value.status === 'ready'));
assert.ok(requests.every(value => !value.options || !value.options.method || value.options.method === 'GET'));
assert.equal(confirmations().promptCount, 0);
assert.equal(confirmations().confirmCount, 0);
assert.equal(state.approvedRevisions.size, 0);
assert.ok(panels.get('0').innerHTML.includes('唯一匹配建议 · 待人工确认'));
""")

    def test_async_matching_preserves_edits_selection_and_focus(self) -> None:
        self.run_ui(r"""
const loading = loadReviews();
requests[0].resolve({items: [item('a'), item('b')]});
await loading;
Object.assign(draft, {editor: '{"confidence": 0.8}', selected: true, editEnabled: true, focused: true});
const writes = elements.reviewList.writes;
requests[1].resolve(resolution('a'));
requests[2].reject(new Error('temporary failure'));
await flush();
assert.equal(elements.reviewList.writes, writes);
assert.equal(draft.editor, '{"confidence": 0.8}');
assert.equal(draft.selected, true);
assert.equal(draft.editEnabled, true);
assert.equal(draft.focused, true);
assert.ok(panels.get('1').innerHTML.includes('重新匹配'));
""")

    def test_same_revision_cache_is_reused_until_explicit_refresh(self) -> None:
        self.run_ui(r"""
let loading = loadReviews();
requests[0].resolve({items: [item('a')]});
await loading;
requests[1].resolve(resolution('a'));
await flush();
loading = loadReviews();
requests[2].resolve({items: [item('a')]});
await loading;
assert.equal(requests.length, 3);
assert.equal(state.resolutions.get('a').status, 'ready');
loading = loadReviews({refreshResolutions: true});
requests[3].resolve({items: [item('a')]});
await loading;
assert.equal(requests.length, 5);
requests[4].resolve(resolution('a', 1, 'NO_MATCH'));
await flush();
assert.ok(panels.get('0').innerHTML.includes('没有匹配目标 · 保留为新实体候选'));
""")

    def test_failed_matching_requires_retry_and_retry_has_no_mutation(self) -> None:
        self.run_ui(r"""
const loading = loadReviews();
requests[0].resolve({items: [item('a')]});
await loading;
requests[1].reject(new Error('dependency unavailable'));
await flush();
assert.equal(state.resolutions.get('a').status, 'error');
assert.ok(panels.get('0').innerHTML.includes('自动匹配失败'));
await flush();
assert.equal(requests.length, 2);
const retry = loadResolution(0, {});
assert.equal(requests.length, 3);
requests[2].resolve(resolution('a'));
await retry;
assert.equal(state.resolutions.get('a').status, 'ready');
assert.ok(requests.every(value => !value.options?.method));
""")

    def test_latest_queue_wins_and_denial_clears_old_matching(self) -> None:
        self.run_ui(r"""
const first = loadReviews();
const second = loadReviews();
requests[1].resolve({items: [item('new')]});
await second;
requests[0].resolve({items: [item('old')]});
await first;
assert.equal(state.reviews[0].record_id, 'new');
assert.equal(requests.length, 3);
const denied = loadReviews();
requests[3].reject(new Error('denied'));
await denied;
requests[2].resolve(resolution('new'));
await flush();
assert.equal(state.reviews.length, 0);
assert.equal(state.resolutions.size, 0);
assert.ok(elements.reviewList.innerHTML.includes('denied'));
const third = loadReviews();
const fourth = loadReviews();
requests[5].resolve({items: []});
await fourth;
requests[4].reject(new Error('stale denied'));
await third;
assert.ok(!elements.reviewList.innerHTML.includes('stale denied'));
""")

    def test_identity_change_discards_inflight_and_waiting_matches(self) -> None:
        self.run_ui(r"""
const loading = loadReviews();
requests[0].resolve({items: [item('a'), item('b'), item('c')]});
await loading;
state.identityEpoch += 1;
invalidateReviewResolutions();
const newQueue = loadReviews();
requests[3].resolve({items: [item('new')]});
await newQueue;
assert.equal(requests.length, 4);
requests[1].resolve(resolution('a'));
requests[2].reject(new Error('old identity denied'));
await flush();
assert.equal(requests.length, 5);
assert.ok(requests[4].url.includes('/new?'));
requests[4].resolve(resolution('new'));
await flush();
assert.equal(state.resolutions.size, 1);
assert.equal(state.resolutions.get('new').status, 'ready');
assert.ok(!requests.some(value => value.url.includes('/c?')));
""")

    def test_record_revision_and_earlier_refresh_responses_cannot_overwrite(self) -> None:
        self.run_ui(r"""
let loading = loadReviews();
requests[0].resolve({items: [item('a')]});
await loading;
loading = loadReviews();
requests[2].resolve({items: [item('a', 2)]});
await loading;
requests[3].resolve(resolution('a', 2, 'NO_MATCH'));
await flush();
requests[1].resolve(resolution('a', 1));
await flush();
assert.equal(state.resolutions.get('a').revision, 2);
assert.equal(state.resolutions.get('a').suggestions[0].outcome, 'NO_MATCH');
const first = loadResolution(0, {});
const second = loadResolution(0, {});
requests[5].resolve(resolution('a', 2, 'CONFLICT'));
await second;
requests[4].reject(new Error('stale matching failure'));
await first;
assert.equal(state.resolutions.get('a').suggestions[0].outcome, 'CONFLICT');
assert.ok(panels.get('0').innerHTML.includes('身份属性冲突 · 保持分离'));
""")

    def test_mismatched_response_is_error_and_stale_result_cannot_apply(self) -> None:
        self.run_ui(r"""
const loading = loadReviews();
requests[0].resolve({items: [item('a')]});
await loading;
requests[1].resolve(resolution('a', 99));
await flush();
assert.equal(state.resolutions.get('a').status, 'error');
const retry = loadResolution(0, {});
requests[2].resolve(resolution('a'));
await retry;
state.reviewEpoch += 1;
await applyResolution(0, 0, {});
assert.equal(confirmations().promptCount, 0);
assert.equal(requests.length, 3);
""")

    def test_applying_current_suggestion_requires_explicit_human_confirmation(self) -> None:
        self.run_ui(r"""
const loading = loadReviews();
requests[0].resolve({items: [item('a')]});
await loading;
requests[1].resolve(resolution('a'));
await flush();
await applyResolution(0, 0, {});
assert.equal(confirmations().promptCount, 1);
assert.equal(requests.length, 2);
assert.equal(state.approvedRevisions.size, 0);
""")

    def test_queue_refresh_discards_superseded_waiting_work(self) -> None:
        self.run_ui(r"""
let loading = loadReviews();
requests[0].resolve({items: [item('a'), item('b'), item('c'), item('d'), item('e')]});
await loading;
assert.equal(state.resolutionQueue.length, 3);
loading = loadReviews({refreshResolutions: true});
assert.equal(state.resolutionQueue.length, 0);
requests[3].resolve({items: [item('a')]});
await loading;
assert.equal(state.resolutionQueue.length, 1);
requests[1].resolve(resolution('a', 1, 'CONFLICT'));
requests[2].resolve(resolution('b'));
await flush();
assert.equal(requests.length, 5);
requests[4].resolve(resolution('a'));
await flush();
assert.equal(state.resolutions.get('a').suggestions[0].outcome, 'AUTO_LINK');
assert.ok(!requests.some(value => /\/[cde]\?/.test(value.url)));
""")

    def test_authority_context_refresh_preserves_drafts_and_drops_stale_matches(self) -> None:
        self.run_ui(r"""
const loading = loadReviews();
requests[0].resolve({items: [item('a'), item('b'), item('c')]});
await loading;
Object.assign(draft, {editor: 'typed review', selected: true, editEnabled: true, focused: true});
const writes = elements.reviewList.writes;
refreshReviewResolutions();
assert.equal(state.resolutionActive, 2);
assert.equal(state.resolutionQueue.length, 3);
assert.equal(requests.length, 3);
requests[1].resolve(resolution('a', 1, 'NO_MATCH'));
requests[2].resolve(resolution('b', 1, 'NO_MATCH'));
await flush();
assert.equal(state.resolutionActive, 2);
assert.equal(requests.length, 5);
assert.equal(state.resolutions.get('a').status, 'loading');
requests[3].resolve(resolution('a'));
requests[4].resolve(resolution('b'));
await flush();
requests[5].resolve(resolution('c'));
await flush();
assert.equal(state.resolutions.get('a').suggestions[0].outcome, 'AUTO_LINK');
assert.equal(elements.reviewList.writes, writes);
assert.equal(draft.editor, 'typed review');
assert.equal(draft.selected, true);
assert.equal(draft.focused, true);
assert.equal(draft.editEnabled, true);
const before = requests.length;
refreshReviewResolutions(state.identityEpoch - 1);
assert.equal(requests.length, before);
""")

    def test_publication_rollback_and_tbox_publish_refresh_cached_matching(self) -> None:
        self.run_ui(r"""
const loading = loadReviews();
requests[0].resolve({items: [item('a')]});
await loading;
requests[1].resolve(resolution('a', 1, 'NO_MATCH'));
await flush();
Object.assign(draft, {editor: 'retained review', selected: true});
const writes = elements.reviewList.writes;
for (const action of [() => publishOntology(0), () => publishKnowledge(), () => rollbackPublication(1)]) {
  elements.publicationRevisions.value = 'approved-1';
  const before = requests.length;
  const mutation = action();
  assert.equal(requests[before].options.method, 'POST');
  requests[before].resolve({tbox_id: 'tbox', publication_id: 'publication', generation: 2});
  await mutation;
  assert.equal(requests.length, before + 2);
  assert.ok(requests[before + 1].url.includes('/entity-resolution/a?'));
  requests[before + 1].resolve(resolution('a'));
  await flush();
  assert.equal(elements.reviewList.writes, writes);
  assert.equal(draft.editor, 'retained review');
  assert.equal(draft.selected, true);
}
""")

    def test_stale_identity_publication_results_cannot_refresh_or_clear_new_form(self) -> None:
        self.run_ui(r"""
for (const action of [() => publishOntology(0), () => publishKnowledge(), () => rollbackPublication(1)]) {
  elements.publicationRevisions.value = 'old-approved';
  const before = requests.length;
  const mutation = action();
  state.identityEpoch += 1;
  elements.publicationRevisions.value = 'new-identity-draft';
  elements.publicationOutput.textContent = 'new-identity-output';
  requests[before].resolve({tbox_id: 'old-tbox', publication_id: 'old-publication', generation: 2});
  await mutation;
  assert.equal(requests.length, before + 1);
  assert.equal(elements.publicationRevisions.value, 'new-identity-draft');
  assert.equal(elements.publicationOutput.textContent, 'new-identity-output');
}
""")
