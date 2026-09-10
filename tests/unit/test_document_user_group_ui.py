"""The temporary user-group picker and implicit administrator access."""
import json
from pathlib import Path
import subprocess
import unittest
from tests.fixtures.workbench_ui import governance_markup


class DocumentUserGroupTests(unittest.TestCase):
    def test_single_dropdown_and_implicit_admin_group(self):
        path=Path('src/graphrag_prod/playground/static/industrial/governance.mjs')
        source=path.read_text()
        begin=source.index('      function renderDocumentAccessGroups()')
        end=source.index('      function parseJsonEditor',begin)
        script='''
import assert from 'node:assert/strict';
let persona={groups:['members','administrators']};
const currentPersona=()=>persona;
const nodes={
  'document-access-groups':{innerHTML:''},'document-access-note':{textContent:''},
  'document-access-group':{value:'members',disabled:false},
};
const $=id=>nodes[id];
'''+source[begin:end]+'''
renderDocumentAccessGroups();
const html=nodes['document-access-groups'].innerHTML;
assert.equal((html.match(/<option /g)||[]).length,1);
assert.ok(html.includes('假想的用户组-1'));
assert.ok(html.includes('<select'));
assert.ok(!html.includes('checkbox')&&!html.includes('仅管理员'));
assert.ok(nodes['document-access-note'].textContent.includes('管理员默认可见'));
assert.deepEqual(selectedDocumentAccessGroups(),['members','administrators']);
persona={groups:['members']};
assert.deepEqual(selectedDocumentAccessGroups(),['members']);
persona={groups:['administrators']};
assert.deepEqual(selectedDocumentAccessGroups(),[]);
persona={groups:['members','administrators']};nodes['document-access-group'].value='unassigned-group';
assert.deepEqual(selectedDocumentAccessGroups(),[]);
'''
        result=subprocess.run(['node','--input-type=module','-e',script],text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_label_and_todo_match_current_design(self):
        markup=governance_markup()
        self.assertIn('<label for="document-access-group">新文档访问组</label>',markup)
        self.assertNotIn('新文档访问组（必须是当前身份访问组的非空子集）',markup)
        todo=Path('to_do_list.md').read_text()
        self.assertIn('实现知识库用户分组与访问授权管理',todo)
        self.assertIn('假想的用户组-1',todo)
