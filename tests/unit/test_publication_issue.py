"""Small checks for safe publication errors and the existing correction UI."""
from dataclasses import asdict
from tests.fixtures.workbench_ui import governance_source

import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from graphrag_prod.api.app import _public_error_response
from graphrag_prod.api.knowledge import Neo4jKnowledgeOperations
from graphrag_prod.api.knowledge_contracts import PublicationRequest
from graphrag_prod.api.runtime import ConflictError, PublicationValidationError
from graphrag_prod.domain import Principal
from graphrag_prod.domain.publication_issue import PublicationIssue, PublicationIssueTarget
from graphrag_prod.knowledge.review import KnowledgePublicationConflict


class PublicationIssueTests(unittest.TestCase):
    def issue(self):
        return PublicationIssue('PROPERTY_VALUES_DIFFER', (
            PublicationIssueTarget('record-one', 'pump-one', '循环水泵', 'RatedPower'),
        ))

    def response(self, error):
        request = SimpleNamespace(state=SimpleNamespace(request_id='request-one'))
        response = _public_error_response(request, error, metrics=Mock())
        self.assertEqual(response.status_code, 409)
        return json.loads(response.body)

    def test_http_error_carries_fixed_reason_and_record_location(self):
        result = self.response(PublicationValidationError(self.issue()))
        self.assertEqual(result['publication_issue']['targets'][0]['record_id'], 'record-one')
        self.assertEqual(result['publication_issue']['reason'], 'PROPERTY_VALUES_DIFFER')
        self.assertIn('只允许一个值', result['publication_issue']['message'])
        self.assertEqual(result['request_id'], 'request-one')

    def test_generic_error_does_not_expose_exception_text_or_cause(self):
        error = ConflictError('private-database-marker')
        error.__cause__ = KnowledgePublicationConflict('private-query-marker', issue=self.issue())
        result = self.response(error)
        self.assertNotIn('publication_issue', result)
        self.assertNotIn('private-', json.dumps(result))

    def test_adapter_preserves_locations_for_preview_and_publish_only(self):
        publications = Mock()
        operations = Neo4jKnowledgeOperations(driver=Mock(), construction=Mock(), publications=publications)
        principal = Principal('publisher', 'tenant', frozenset({'group'}), frozenset({'knowledge:publish'}))
        request = PublicationRequest(approved_revision_ids=('revision-one',), expected_active_publication_id=None)
        for preview_only in (True, False):
            publications.publish.side_effect = KnowledgePublicationConflict('private-backend-detail', issue=self.issue())
            with self.assertRaises(PublicationValidationError) as raised:
                operations.publish(principal, request, preview_only=preview_only)
            self.assertEqual(raised.exception.issue, self.issue())
            self.assertNotIn('private-backend-detail', json.dumps(self.response(raised.exception)))
        publications.publish.side_effect = KnowledgePublicationConflict('private-backend-detail')
        with self.assertRaises(ConflictError) as raised:
            operations.publish(principal, request, preview_only=True)
        self.assertNotIn('publication_issue', self.response(raised.exception))

    def test_locations_are_bounded_and_reason_is_allowlisted(self):
        with self.assertRaises(ValueError):
            PublicationIssue('some-exception-text')
        with self.assertRaises(ValueError):
            PublicationIssue('PROPERTY_VALUES_DIFFER', self.issue().targets * 51)

    def test_page_marks_exact_record_expands_and_keeps_selection(self):
        page = governance_source()
        functions = page[page.index('function clearPublicationIssue('):page.index('function invalidatePublicationPreview(')]
        start = page.index('const escapeHtml =')
        escape = page[start:page.index('\n', start)]
        script = r'''
const assert=require('node:assert/strict');
const payload=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const state={publicationCandidates:[],selectedCandidateRevisions:new Set(['revision-one','revision-two'])};
const section={tagName:'DETAILS',open:false,parentElement:null};
function row(record) {
  const classes=new Set();const note={hidden:true,textContent:''};
  return {dataset:{publicationRecord:record,publicationEntityId:'pump-one',publicationPredicate:'RatedPower'},
    parentElement:section,classes,note,
    classList:{add:x=>classes.add(x),remove:x=>classes.delete(x)},
    querySelector:()=>note,scrollIntoView(){this.scrolled=true;},focus(){this.focused=true;}};
}
const first=row('record-one'),other=row('record-two');
const root={querySelectorAll(selector){
  if(selector==='[data-publication-record]')return [first,other];
  if(selector==='[data-publication-problem-note]')return [first.note,other.note];
  if(selector==='.publication-problem')return [first,other].filter(n=>n.classes.has('publication-problem'));
  return [];
}};
section.parentElement=root;
const locate={dataset:{publicationLocate:'0'}};
const panel={innerHTML:'',querySelectorAll:()=>[locate]};
const document={getElementById:()=>panel};
const $=id=>document.getElementById(id);
const elements={publicationCandidateList:root};
function reviewPropertyLabel(value){return value;}
function reviewEntity(item){return item.subject;}
function reviewFactText(item){return item.title;}
eval(payload.escape+'\n'+payload.functions);
state.publicationCandidates=[{record:{record_id:'record-one',title:'额定功率：45 kW',subject:{canonical_name:'循环水泵'}}}];
showPublicationIssue({publicationIssue:payload.issue});
assert.equal(section.open,true);
assert.ok(first.classes.has('publication-problem'));
assert.ok(!other.classes.has('publication-problem'));
assert.equal(first.note.hidden,false);
assert.ok(panel.innerHTML.includes('循环水泵'));
assert.ok(panel.innerHTML.includes('额定功率：45 kW'));
assert.ok(panel.innerHTML.includes('返回修改'));
assert.deepEqual([...state.selectedCandidateRevisions],['revision-one','revision-two']);
locate.onclick();assert.ok(first.scrolled && first.focused);
clearPublicationIssue();assert.equal(first.note.hidden,true);assert.equal(first.classes.size,0);
const malicious={...payload.issue,targets:[{record_id:'missing',entity_name:'<img src=x onerror=alert(1)>',predicate:'RatedPower'}]};
showPublicationIssue({publicationIssue:malicious});
assert.ok(!panel.innerHTML.includes('<img'));
assert.ok(panel.innerHTML.includes('不在当前候选列表'));
assert.ok(!first.classes.has('publication-problem'));
showPublicationIssue({message:'暂不可用',payload:{request_id:'request-one'}});
assert.ok(panel.innerHTML.includes('request-one'));
assert.equal(state.publicationIssue,null);
'''
        issue = {**asdict(self.issue()), 'message': self.issue().message}
        result = subprocess.run(['node', '-e', script], input=json.dumps({'functions': functions, 'escape': escape, 'issue': issue}),
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
