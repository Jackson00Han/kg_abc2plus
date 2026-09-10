"""Executable workflow checks for ordered setup and scoped expert publication."""

from __future__ import annotations

from tests.fixtures.workbench_ui import governance_source

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
        self.parents: list[tuple[str, str]] = []
        self.by_id: dict[str, dict[str, str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if "id" in values or "data-construction-flow" in values or "data-flow-choice" in values:
            values["parent_id"] = next((node_id for _, node_id in reversed(self.parents) if node_id), "")
            self.nodes.append(values)
            if "id" in values:
                self.by_id[values["id"]] = values
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.parents.append((tag, values.get("id", "")))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.parents) - 1, -1, -1):
            if self.parents[index][0] == tag:
                del self.parents[index:]
                return

    def handle_data(self, data: str) -> None:
        for _, node_id in self.parents:
            if node_id:
                values = self.by_id[node_id]
                values["text_content"] = values.get("text_content", "") + data


class PlaygroundFlowUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = governance_source()
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
  const element = {attrs,id:attrs.id,dataset: {}, value: '', textContent: attrs.text_content || '', innerHTML: '',
    children:[],classList:{toggle(){}},disabled: false, hidden: Object.hasOwn(attrs, 'hidden'), parent: null,
    setAttribute(name, value) { this.attrs[name] = value; },
    appendChild(child) { child.parent = this; },
    scrollIntoView() { this.scrolled = true; },
    addEventListener(name, fn) { this[name] = fn; },
    querySelectorAll(selector) { return selector===':scope > section'?nodes.filter(node=>(node.attrs.id||'').startsWith('maintenance-')):[]; },
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
for (const node of nodes) node.parent = fields.get(node.attrs.parent_id) || null;
const $ = id => { assert.ok(fields.has(id), 'Unknown page control: ' + id); return fields.get(id); };
const document = {querySelectorAll(selector) {
  return nodes.filter(node => Object.hasOwn(node.attrs, selector.slice(1, -1)));
}};
const state = {identityEpoch: 0, ontologies: [], constructionFlow: 'baseline',
  uploadKnowledgeScope: 'BUSINESS',
  constructionBusy: false, expertImportBusy: false, expertPublishing: false,
  expertRevisionIds: [], approvedRevisions: new Set(['unrelated-approved']),
  selectedCandidateRevisions: new Set(['unrelated-selected']), bootstrap: null};
const elements = {ontologyEditor: $('ontology-editor'), aboxEditor: $('abox-editor'), aboxOutput: $('abox-output'),
  constructionOutput: $('construction-output'), publicationRevisions: $('publication-revisions')};
$('document-extraction-mode').value='LLM';
$('document-knowledge-scope').value='BUSINESS';
elements.publicationRevisions.value = 'unrelated-approved';
elements.aboxEditor.value = JSON.stringify({mentions: [], assertions: []});
const requests = [];
const context = vm.createContext({$, fields, document,host:document,lastBuildFlow:'business',buildStep:'upload',onNavigate(){},requestAnimationFrame:fn=>fn(),state, elements, assert, requests,
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
state.identityEpoch++;state.ontologySaving=false; $('document-tbox').value='new-identity';
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
state.identityEpoch++;state.manualBusy=false; $('manual-output').textContent='new identity';
requests[3].resolve({chunks:[{mention_record_ids:['record']} ]}); await retry;
assert.equal($('manual-output').textContent,'new identity'); assert.equal(state.manualBusy,false);
""")

    def test_unified_instance_workflow_keeps_one_fixed_control_per_step(self) -> None:
        ids = [item["id"] for item in self.nodes if "id" in item]
        self.assertEqual(len(ids), len(set(ids)))
        steps = [
            "step-ontology", "source-upload-slot", "upload-card",
            "business-review-slot", "step-review", "business-publication-slot", "step-publication",
        ]
        self.assertEqual([ids.index(step) for step in steps], sorted(ids.index(step) for step in steps))
        for retired in ("business-upload-slot", "step-abox", "baseline-publication-slot"):
            self.assertNotIn(retired, ids)
        self.assertFalse(any("data-flow-choice" in item for item in self.nodes))
        self.run_js(r"""
const upload = $('upload-card');
const next = makeNode();
bindReviewActions({querySelectorAll: selector => selector === '[data-review-next]' ? [next] : []});
showConstructionFlow('baseline');
assert.equal(upload.parent, $('source-upload-slot'));
assert.equal($('step-ontology').hidden, false);
assert.equal($('step-review').hidden, true);
assert.equal($('upload-card').hidden, false);
assert.equal($('step-review').parent, $('business-review-slot'));
assert.equal($('step-publication').parent, $('business-publication-slot'));
assert.equal($('document-extraction-mode').value, 'LLM');
assert.equal($('document-knowledge-scope').value, 'BUSINESS');
assert.match($('construction-flow-steps').innerHTML, /01 上传资料/);
assert.match($('construction-flow-steps').innerHTML, /02 复核候选/);
assert.match($('construction-flow-steps').innerHTML, /03 发布知识/);
next.click();
assert.equal(state.constructionFlow, 'baseline');
assert.equal($('step-publication').parent, $('business-publication-slot'));
assert.equal($('publication-title').textContent, '发布知识');
assert.ok(!$('step-publication').scrolled);
$('document-file').value = 'expert.txt';
showConstructionFlow('business', 'step-review');
assert.equal($('upload-card'), upload);
assert.equal(upload.parent, $('source-upload-slot'));
assert.equal($('step-ontology').hidden, false);
assert.equal($('step-review').hidden, false);
assert.ok(!$('step-review').scrolled);
assert.equal($('document-extraction-mode').value, 'LLM');
assert.equal($('document-file').value, 'expert.txt'); // Changing declared source type preserves the explicit draft.
assert.equal($('instance-workspace').hidden, false);
assert.equal($('step-review').parent, $('business-review-slot'));
assert.equal($('step-publication').parent, $('business-publication-slot'));
next.click();
assert.equal(state.constructionFlow, 'business');
assert.equal($('step-publication').parent, $('business-publication-slot'));
assert.equal($('upload-title').textContent, '上传资料');
assert.equal($('review-title').textContent, '复核候选');
assert.equal($('publication-title').textContent, '发布知识');
assert.ok(!$('step-publication').scrolled);
assert.equal(elements.publicationRevisions.value, 'unrelated-approved');
assert.equal(state.approvedRevisions.has('unrelated-approved'), true);
state.constructionBusy = true;
showConstructionFlow('baseline', 'step-review');
assert.equal(state.constructionFlow, 'baseline');
assert.equal($('step-review').hidden, false);
assert.equal($('document-extraction-mode').value, 'LLM');
state.constructionBusy = false;
showConstructionFlow('maintenance');
assert.equal($('maintenance-inventory').hidden, true);
assert.equal($('maintenance-quality').hidden, false);
assert.equal($('business-review-slot').hidden, true);
assert.equal(requests.length, 0);
""")

    def test_peer_tabs_preserve_drafts_step_and_choose_default_from_ontology(self) -> None:
        self.run_js(r"""
state.buildView=null;
state.ontologies=[];
showConstructionFlow('business');
assert.equal($('expert-foundation').hidden,false);
assert.equal($('instance-workspace').hidden,true);
state.ontologies=[{key:'pump-maintenance-demo',version:1,status:'PUBLISHED'}];
showConstructionFlow('business');
assert.equal($('expert-foundation').hidden,true);
assert.equal($('instance-workspace').hidden,false);
assert.equal($('build-tab-instances').attrs['aria-selected'],'true');
assert.equal($('build-tab-instances').tabIndex,0);
const ontologyDraft=elements.ontologyEditor.value='unsaved definition';
$('document-title').value='循环水泵维护资料';
setUploadKnowledgeScope('AUTHORITATIVE');
showConstructionFlow('business','step-review');
selectBuildView('ontology');
assert.equal($('expert-foundation').hidden,false);
assert.equal($('instance-workspace').hidden,true);
assert.equal($('build-tab-ontology').attrs['aria-selected'],'true');
assert.equal($('build-tab-instances').tabIndex,-1);
showConstructionFlow('browse');
assert.equal($('expert-foundation').hidden,true);
assert.equal($('instance-workspace').hidden,true);
showConstructionFlow('business');
assert.equal($('expert-foundation').hidden,false);
selectBuildView('instances');
assert.equal($('step-review').hidden,false);
assert.equal($('upload-card').hidden,true);
assert.equal(elements.ontologyEditor.value,ontologyDraft);
assert.equal($('document-title').value,'循环水泵维护资料');
assert.equal($('document-knowledge-scope').value,'AUTHORITATIVE');
assert.equal(state.approvedRevisions.has('unrelated-approved'),true);
selectBuildView('invalid');
assert.equal(state.buildView,'instances');
$('document-tbox').value='pump-maintenance-demo';
renderConstructionOntology();
assert.equal($('foundation-status').textContent,'pump-maintenance-demo · v1');
$('document-tbox').value='pump-maintenance-demo-draft';
renderConstructionOntology();
assert.match($('foundation-status').textContent,/未启用/);
assert.equal($('inspect-build-ontology').textContent,'配置');
assert.equal(requests.length,0);
""")

    def test_source_selection_preserves_drafts_reviews_and_publication_selection(self) -> None:
        self.run_js(r"""
$('document-file').value = 'pump-notes.txt';
$('document-title').value = '循环水泵资料';
$('document-uri').value = 'urn:pump:notes';
$('document-extraction-mode').value = 'SOURCE_ONLY';
state.reviews = [{revision_id: 'pending-review'}];
const reviews = state.reviews;
const draft = elements.aboxEditor.value;
for (const scope of ['AUTHORITATIVE', 'BUSINESS']) {
  $('document-knowledge-scope').value = scope;
  setUploadKnowledgeScope(scope);
  showConstructionFlow('baseline', 'step-review');
  showConstructionFlow('business', 'step-publication');
  showConstructionFlow('baseline', 'step-ontology');
  showConstructionFlow('business', 'business-upload-slot');
  assert.equal($('document-knowledge-scope').value, scope);
  assert.equal(state.uploadKnowledgeScope, scope);
  assert.equal($('document-file').value, 'pump-notes.txt');
  assert.equal($('document-title').value, '循环水泵资料');
  assert.equal($('document-uri').value, 'urn:pump:notes');
  assert.equal($('document-extraction-mode').value, 'SOURCE_ONLY');
  assert.equal(elements.aboxEditor.value, draft);
  assert.equal(state.reviews, reviews);
  assert.equal(state.approvedRevisions.has('unrelated-approved'), true);
  assert.equal(state.selectedCandidateRevisions.has('unrelated-selected'), true);
  assert.equal(elements.publicationRevisions.value, 'unrelated-approved');
  assert.equal($('upload-card').parent, $('source-upload-slot'));
  assert.equal($('upload-card').hidden, false);
}
state.constructionBusy = true;
$('document-knowledge-scope').value = 'AUTHORITATIVE';
setUploadKnowledgeScope('AUTHORITATIVE');
assert.equal($('document-knowledge-scope').value, 'BUSINESS');
assert.equal(state.uploadKnowledgeScope, 'BUSINESS');
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
assert.equal(state.buildView, 'ontology');
assert.equal($('expert-foundation').hidden, false);
assert.equal($('instance-workspace').hidden, true);
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
state.expertPublishing = false; // The real identity reset releases the old operation.
requests[5].resolve({items: []});
requests[6].resolve({items: [{record: {revision_id: 'missing-expert', record_id: 'old-tenant-record'}}]});
await pending;
assert.equal(requests.length, 7);
const importing = importABox();
state.identityEpoch++;
state.expertImportBusy = false;
requests[7].resolve({revision_ids: ['old-tenant-import']});
await importing;
assert.equal(state.expertRevisionIds.length, 0);
""")
