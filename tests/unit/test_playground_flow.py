"""Executable workflow checks for ordered setup and scoped expert publication."""

from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess
import unittest


class PageNodes(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.nodes: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if "id" in values or "data-construction-flow" in values or "data-flow-choice" in values:
            self.nodes.append(values)


class PlaygroundFlowUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (
            Path(__file__).parents[2]
            / "src/graphrag_prod/playground/static/index.html"
        ).read_text(encoding="utf-8")
        parser = PageNodes()
        parser.feed(cls.source)
        cls.nodes = parser.nodes

    def run_js(self, scenario: str) -> None:
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node is required for executable UI validation")
        code = self.source[
            self.source.index("async function importABox()"):
            self.source.index("function demoKit()")
        ] + self.source[
            self.source.index("function updateConstructionMode()"):
            self.source.index("function constructionChunkSummary(item)")
        ] + self.source[
            self.source.index("function activeOntology(key)"):
            self.source.index("function renderOntologies()")
        ]
        code += self.source[self.source.index("async function importOntology()"):self.source.index("async function publishOntology(")]
        code += self.source[self.source.index("async function submitManualFact()"):self.source.index("function updateConstructionMode()")]
        code += "\n" + "\n".join(
            line for line in self.source.splitlines()
            if line.strip().startswith("elements.aboxEditor.addEventListener(")
        )
        code += self.source[
            self.source.index("function refreshABoxPreparation("):
            self.source.index("function clearDemoSourceBinding()")
        ]
        code += self.source[
            self.source.index("function bindReviewActions(container)"):
            self.source.index("function updateReviewAvailability(item)")
        ]
        harness = r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const fields = new Map();
function makeNode(attrs = {}) {
  const element = {attrs, dataset: {}, value: '', textContent: '', innerHTML: '',
    disabled: false, hidden: Object.hasOwn(attrs, 'hidden'), parent: null,
    setAttribute(name, value) { this.attrs[name] = value; },
    appendChild(child) { child.parent = this; },
    scrollIntoView() { this.scrolled = true; },
    addEventListener(name, fn) { this[name] = fn; },
    querySelectorAll() { return []; },
    querySelector(selector) { this.parts ??= new Map(); if (!this.parts.has(selector)) this.parts.set(selector,makeNode()); return this.parts.get(selector); },
  };
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith('data-')) element.dataset[key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = value;
  }
  return element;
}
const nodes = input.nodes.map(attrs => {
  const element = makeNode(attrs);
  if (attrs.id) fields.set(attrs.id, element);
  return element;
});
const $ = id => { assert.ok(fields.has(id), 'Unknown page control: ' + id); return fields.get(id); };
const document = {querySelectorAll(selector) {
  return nodes.filter(node => Object.hasOwn(node.attrs, selector.slice(1, -1)));
}};
const state = {identityEpoch: 0, ontologies: [], constructionFlow: 'baseline',
  constructionBusy: false, expertImportBusy: false, expertPublishing: false,
  expertRevisionIds: [], approvedRevisions: new Set(['unrelated-approved']),
  selectedCandidateRevisions: new Set(['unrelated-selected']), bootstrap: null};
const elements = {ontologyEditor: $('ontology-editor'), aboxEditor: $('abox-editor'), aboxOutput: $('abox-output'),
  constructionOutput: $('construction-output'), publicationRevisions: $('publication-revisions')};
elements.publicationRevisions.value = 'unrelated-approved';
elements.aboxEditor.value = JSON.stringify({mentions: [], assertions: []});
const requests = [];
const context = vm.createContext({$, fields, document, state, elements, assert, requests,
  parseJsonEditor: element => JSON.parse(element.value),
  renderOntologies() {}, showToast() {}, invalidateInventory() {}, refreshReviewResolutions() {},
  bindResolutionActions() {}, makeNode,
  output: (element, value) => {element.textContent = typeof value === 'string' ? value : JSON.stringify(value);},
  loadPublicationCandidates: async () => {}, loadHistory: async () => {},
  loadInventory: async () => {}, loadQuality: async () => {},
  loadQualityHistory: async () => {}, loadActiveDocuments: async () => {},
  apiRequest: (url, options) => new Promise((resolve, reject) => requests.push({url, options, resolve, reject})),
  crypto: require('node:crypto').webcrypto,
  selectedDocumentAccessGroups: () => ['engineers'],
  loadOntologies: async () => {}, loadReviews: async () => {},
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

    def test_atomic_ontology_save_uses_real_definition_and_discards_stale_identity(self) -> None:
        self.run_js(r"""
state.ontologies=[{key:'assets', tbox_id:'old-active',status:'PUBLISHED'}];
const definition={key:'assets',version:2,entity_types:[],relationship_types:[],expected_checksum:'a'.repeat(64)};
elements.ontologyEditor.value=JSON.stringify({schema:'graphrag-property-tbox-export-v1',checksum:definition.expected_checksum,definition});
const first=importOntology(); const duplicate=importOntology();
assert.equal(requests.length,1);
const sent=JSON.parse(requests[0].options.body);
assert.equal(sent.activate,true); assert.equal(sent.expected_active_tbox_id,'old-active');
assert.equal(sent.expected_checksum,definition.expected_checksum); assert.equal(sent.version,2);
state.identityEpoch++; $('document-tbox').value='new-identity';
requests[0].resolve({tbox_id:'new-active'}); await Promise.all([first,duplicate]);
assert.equal($('document-tbox').value,'new-identity'); assert.equal(state.ontologySaving,false);
elements.ontologyEditor.value=JSON.stringify({schema:'unsupported',definition}); await importOntology();
assert.equal(requests.length,1);
""")

    def test_manual_form_keeps_retry_identity_and_never_sends_authority_or_document_text(self) -> None:
        self.run_js(r"""
$('document-tbox').value='assets'; $('manual-kind').value='ENTITY';
$('manual-subject-type').value='Equipment'; $('manual-subject-name').value='Pump-7';
const first=submitManualFact(); const duplicate=submitManualFact();
assert.equal(requests.length,1);
requests[0].resolve({items:[{key:'assets',tbox_id:'tbox',status:'PUBLISHED'}]}); await flush();
const sent=JSON.parse(requests[1].options.body);
assert.equal(sent.extraction_mode,'MANUAL'); assert.equal(sent.source_name,'人工补充记录');
assert.equal(sent.canonical_uri,'urn:graphrag:human:'+sent.operation_key);
assert.equal(sent.knowledge_scope,undefined); assert.equal(sent.content_base64,undefined);
requests[1].reject(new Error('unknown network outcome')); await Promise.all([first,duplicate]);
const retry=submitManualFact(); requests[2].resolve({items:[{key:'assets',tbox_id:'tbox',status:'PUBLISHED'}]}); await flush();
assert.equal(JSON.parse(requests[3].options.body).operation_key,sent.operation_key);
state.identityEpoch++; $('manual-output').textContent='new identity';
requests[3].resolve({chunks:[{mention_record_ids:['record']} ]}); await retry;
assert.equal($('manual-output').textContent,'new identity'); assert.equal(state.manualBusy,false);
""")

    def test_workflow_order_and_shared_upload_preserve_one_control_per_id(self) -> None:
        ids = [item["id"] for item in self.nodes if "id" in item]
        self.assertEqual(len(ids), len(set(ids)))
        steps = [
            "step-ontology", "source-upload-slot", "step-abox",
            "business-upload-slot", "step-review", "step-publication",
        ]
        self.assertEqual([ids.index(step) for step in steps], sorted(ids.index(step) for step in steps))
        self.run_js(r"""
const upload = $('upload-card');
const next = makeNode();
bindReviewActions({querySelectorAll: selector => selector === '[data-review-next]' ? [next] : []});
showConstructionFlow('baseline');
assert.equal(upload.parent, $('source-upload-slot'));
assert.equal($('step-ontology').hidden, false);
assert.equal($('step-review').hidden, false);
assert.equal($('step-review').parent, $('step-abox'));
assert.equal($('step-publication').parent, $('baseline-publication-slot'));
assert.equal($('document-extraction-mode').value, 'LLM');
next.click();
assert.equal(state.constructionFlow, 'baseline');
assert.equal($('step-publication').parent, $('baseline-publication-slot'));
assert.equal($('step-publication').querySelector('.step-chip').textContent, '04');
assert.equal($('step-publication').querySelector('h2').textContent, '发布权威图谱');
assert.equal($('step-publication').scrolled, true);
$('step-publication').scrolled = false;
$('document-file').value = 'expert.txt';
showConstructionFlow('business', 'step-review');
assert.equal($('upload-card'), upload);
assert.equal(upload.parent, $('business-upload-slot'));
assert.equal($('step-ontology').hidden, true);
assert.equal($('step-review').hidden, false);
assert.equal($('step-review').scrolled, true);
assert.equal($('document-extraction-mode').value, 'LLM');
assert.equal($('document-file').value, '');
assert.equal($('upload-step-number').textContent, '05');
assert.equal($('step-review').parent, $('business-review-slot'));
assert.equal($('step-publication').parent, $('business-publication-slot'));
next.click();
assert.equal(state.constructionFlow, 'business');
assert.equal($('step-publication').parent, $('business-publication-slot'));
assert.equal($('step-publication').querySelector('.step-chip').textContent, '07');
assert.equal($('step-publication').querySelector('h2').textContent, '发布业务知识');
assert.equal($('step-publication').scrolled, true);
assert.equal(elements.publicationRevisions.value, 'unrelated-approved');
assert.equal(state.approvedRevisions.has('unrelated-approved'), true);
state.constructionBusy = true;
showConstructionFlow('baseline');
assert.equal(state.constructionFlow, 'business');
assert.equal($('document-extraction-mode').value, 'LLM');
state.constructionBusy = false;
showConstructionFlow('maintenance');
assert.equal($('maintenance-inventory').hidden, false);
assert.equal($('step-review').hidden, true);
assert.equal(requests.length, 0);
""")

    def test_unpublished_ontology_preflight_explains_required_action_without_upload(self) -> None:
        self.run_js(r"""
const pending = requirePublishedConstructionOntology('pump', 0);
requests[0].resolve({items: [{key: 'pump', status: 'DRAFT'}]});
await assert.rejects(pending, /尚未启用.*保存并启用/);
assert.equal(requests.length, 1);
assert.equal(requests[0].options, undefined);
assert.equal($('construction-next').hidden, false);
$('construction-next-button').onclick();
assert.equal(state.constructionFlow, 'baseline');
assert.equal($('step-ontology').scrolled, true);
const published = requirePublishedConstructionOntology('pump', 0);
requests[1].resolve({items: [{key: 'pump', status: 'PUBLISHED'}]});
assert.equal(await published, true);
const stale = requirePublishedConstructionOntology('pump', 0);
state.identityEpoch++;
requests[2].resolve({items: [{key: 'other-tenant', status: 'PUBLISHED'}]});
assert.equal(await stale, false);
assert.equal(state.ontologies[0].key, 'pump');
""")

    def test_expert_import_requires_explicit_scoped_publication_and_keeps_business_selection(self) -> None:
        self.run_js(r"""
const importing = importABox();
assert.equal(requests[0].url, '/v1/knowledge/authoritative:import');
requests[0].resolve({revision_ids: ['expert-1', 'expert-2']});
await importing;
assert.equal(requests.length, 1);
assert.equal($('abox-publication-panel').hidden, false);
assert.equal(elements.publicationRevisions.value, 'unrelated-approved');
const publishing = publishImportedABox();
assert.equal(requests.length, 3);
requests[1].resolve({items: [{status: 'ACTIVE', publication_id: 'active-generation', published_revision_ids: ['old-expert']}]});
requests[2].resolve({items: [
  {record: {revision_id: 'expert-1', record_id: 'expert-record-1'}, requires_replacement: true},
  {record: {revision_id: 'expert-2', record_id: 'expert-record-2'}, requires_replacement: false},
  {record: {revision_id: 'unrelated-approved', record_id: 'business-record'}, requires_replacement: true},
]});
await flush();
const body = JSON.parse(requests[3].options.body);
assert.deepEqual(body.approved_revision_ids, ['expert-1', 'expert-2']);
assert.equal(body.expected_active_publication_id, 'active-generation');
assert.deepEqual(body.replace_record_ids, ['expert-record-1']);
assert.deepEqual(body.remove_record_ids, []);
requests[3].resolve({publication_id: 'next-generation'});
await publishing;
assert.equal(state.approvedRevisions.has('unrelated-approved'), true);
assert.equal(state.selectedCandidateRevisions.has('unrelated-selected'), true);
assert.equal(elements.publicationRevisions.value, 'unrelated-approved');
assert.equal($('abox-next-button').hidden, false);
assert.equal($('abox-publish-button').hidden, true);
assert.equal(state.expertRevisionIds.length, 0);
""")

    def test_large_expert_import_publishes_receipt_without_requiring_unrelated_candidate_page(self) -> None:
        self.run_js(r"""
state.expertRevisionIds = Array.from({length: 101}, (_, index) => 'expert-' + index);
const pending = publishImportedABox();
requests[0].resolve({items: []});
requests[1].resolve({items: Array.from({length: 100}, (_, index) => ({record: {revision_id: 'unrelated-' + index, record_id: 'other-' + index}}))});
await flush();
const body = JSON.parse(requests[2].options.body);
assert.equal(body.approved_revision_ids.length, 101);
assert.equal(body.approved_revision_ids.every(id => id.startsWith('expert-')), true);
assert.equal(body.replace_record_ids.length, 0);
requests[2].resolve({publication_id: 'expert-generation'});
await pending;
assert.equal($('abox-next-button').hidden, false);
state.expertRevisionIds = ['previous-import'];
$('abox-publication-panel').hidden = false;
elements.aboxEditor.value = 'invalid JSON';
await importABox();
assert.equal(state.expertRevisionIds.length, 0);
assert.equal($('abox-publication-panel').hidden, true);
await publishImportedABox();
assert.equal(requests.length, 3);
state.expertRevisionIds = ['updated-expert'];
state.expertImportMayReplace = true;
const uncertainUpdate = publishImportedABox();
requests[3].resolve({items: [{status: 'ACTIVE', publication_id: 'current', published_revision_ids: []}]});
requests[4].resolve({items: []});
await uncertainUpdate;
assert.equal(requests.length, 5);
assert.match($('abox-publication-note').textContent, /避免遗漏替换，尚未提交发布/);
// Editing a successfully imported draft makes its receipt unavailable.
state.expertRevisionIds = ['successful-previous-import'];
$('abox-publication-panel').hidden = false;
elements.aboxEditor.value = JSON.stringify({mentions: [], assertions: []});
$('abox-editor').input();
assert.equal(state.expertRevisionIds.length, 0);
assert.equal($('abox-publication-panel').hidden, true);
// An in-flight import cannot restore the old receipt after the draft changes.
const inFlight = importABox();
elements.aboxEditor.value = JSON.stringify({mentions: [], assertions: [], review_notes: 'changed draft'});
$('abox-editor').input();
requests[5].resolve({revision_ids: ['older-draft-result']});
await inFlight;
assert.equal(state.expertRevisionIds.length, 0);
assert.equal($('abox-publication-panel').hidden, true);
assert.match(elements.aboxOutput.textContent, /编辑器内容已改变/);
""")

    def test_expert_publication_handles_already_active_missing_and_stale_results(self) -> None:
        self.run_js(r"""
state.expertRevisionIds = ['expert-1'];
let pending = publishImportedABox();
requests[0].resolve({items: [{status: 'ACTIVE', publication_id: 'active', published_revision_ids: ['expert-1']}]});
requests[1].resolve({items: []});
await pending;
assert.equal(requests.length, 2);
assert.equal($('abox-next-button').hidden, false);
state.expertRevisionIds = ['missing-expert'];
pending = publishImportedABox();
requests[2].resolve({items: []});
requests[3].resolve({items: []});
await flush();
requests[4].reject(new Error('记录已变化'));
await pending;
assert.match($('abox-publication-note').textContent, /记录已变化/);
assert.equal(requests.length, 5);
assert.equal(state.expertRevisionIds[0], 'missing-expert');
pending = publishImportedABox();
state.identityEpoch++;
state.expertRevisionIds = [];
requests[5].resolve({items: []});
requests[6].resolve({items: [{record: {revision_id: 'missing-expert', record_id: 'old-tenant-record'}}]});
await pending;
assert.equal(requests.length, 7);
const importing = importABox();
state.identityEpoch++;
requests[7].resolve({revision_ids: ['old-tenant-import']});
await importing;
assert.equal(state.expertRevisionIds.length, 0);
""")
