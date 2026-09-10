"""Offline checks for the industrial workbench's complete source library."""
from pathlib import Path
import json
import shutil
import subprocess
import unittest


MODULE = (Path(__file__).parents[2] / "src/graphrag_prod/playground/static/industrial/source-catalog.mjs")


class IndustrialSourceCatalogTests(unittest.TestCase):
    def run_module(self, scenario):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node is required for source catalogue checks")
        harness = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
const input=JSON.parse(fs.readFileSync(0,'utf8'));
class Element {
  constructor(tag='div'){this.tagName=tag;this.children=[];this.events={};this.attributes={};this.open=false;this._text='';}
  set textContent(value){this._text=String(value);this.children=[];}
  get textContent(){return this._text+this.children.map(n=>n.textContent??String(n)).join('');}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this._text='';this.children=children;}
  insertAdjacentElement(){} remove(){this.removed=true;}
  setAttribute(k,v){this.attributes[k]=v;}
  addEventListener(k,f){this.events[k]=f;}
  showModal(){this.open=true;}
  close(){if(this.open){this.open=false;this.events.close?.();}}
}
globalThis.document={createElement:tag=>new Element(tag),body:new Element('body')};
const {SourceCatalog,mergeSourcePage}=await import(input.module);
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return{promise,resolve,reject};};
const source=(id='doc',version='v1')=>({document_id:id,version_id:version,version_number:1,title:'维护记录',source_name:'现场上传',canonical_uri:'urn:local:maintenance.txt',chunk_count:2,has_published_knowledge:false});
const chunk=(ordinal=0,version='v1')=>({...source('doc',version),ordinal,chunk_id:'chunk-'+ordinal,char_start:0,char_end:4,text:'泵体温度',checksum:'a'.repeat(64)});
const make=(request,onSelect)=>{const client={epoch:1,request},list=new Element(),summary=new Element();return {client,list,summary,catalog:new SourceCatalog({client,list,summary,onSelect})};};
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
await new AsyncFunction('assert','SourceCatalog','mergeSourcePage','deferred','source','chunk','make','Element',input.scenario)(assert,SourceCatalog,mergeSourcePage,deferred,source,chunk,make,Element);
"""
        result = subprocess.run(
            [node, "--input-type=module", "-e", harness],
            input=json.dumps({"module": MODULE.as_uri(), "scenario": scenario}),
            capture_output=True, text=True, timeout=15, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_metadata_only_joins_the_same_document_and_version(self):
        self.run_module(r"""
const sources=[source('doc'),source('ordinary')];const before=JSON.stringify(sources);
const rows=mergeSourcePage(sources,[{...source('doc','old'),family:'canalis-kt',asset_keys:['hidden-old']}]);
assert.equal(rows[0].family,null);assert.deepEqual(rows[1].asset_keys,[]);
const matched=mergeSourcePage(sources,[{...source('doc'),family:'canalis-kt',asset_keys:['BC-P-101'],source_kind:'USER_UPLOAD'}]);
assert.equal(matched[0].family,'canalis-kt');assert.equal(matched[0].has_published_knowledge,false);
assert.equal(JSON.stringify(sources),before);
""")

    def test_all_documents_page_even_if_industrial_metadata_fails(self):
        self.run_module(r"""
const calls=[];const {catalog,list,summary}=make(async(path,body)=>{
 calls.push({path,body});
 if(path.includes('/industrial/'))throw new Error('optional catalogue unavailable');
 return body.after?{items:[source('z')],has_more:false,next_after:null}:{items:[source('a')],has_more:true,next_after:'a'};
});
await catalog.load();assert.equal(list.children.length,1);assert.equal(catalog.rows[0].document_id,'a');
assert.equal(catalog.rows[0].family,null);assert.equal(catalog.nextButton.disabled,false);
assert.match(summary.textContent,/产品范围信息暂未载入/);
await catalog.readPage(catalog.next,[catalog.after]);
assert.equal(catalog.rows[0].document_id,'z');assert.equal(catalog.previousButton.disabled,false);
assert.equal(catalog.nextButton.disabled,true);
assert.deepEqual(calls.filter(c=>c.path.includes('/knowledge/')).map(c=>c.body),[{limit:30},{limit:30,after:'a'}]);
""")

    def test_identity_reset_discards_late_document_pages(self):
        self.run_module(r"""
const pending=deferred();const {catalog,list,summary,client}=make(path=>path.includes('/industrial/')?Promise.resolve({sources:[],has_more:false}):pending.promise);
const loading=catalog.load();client.epoch++;catalog.reset();
pending.resolve({items:[source('previous-persona')],has_more:false,next_after:null});
await loading;assert.equal(list.children.length,0);assert.equal(catalog.rows.length,0);
assert.doesNotMatch(summary.textContent,/previous-persona/);assert.equal(catalog.nextButton.disabled,true);
""")

    def test_family_filter_keeps_catalogue_scope_and_reports_bounds(self):
        self.run_module(r"""
const calls=[];const {catalog,summary}=make(async(path,body)=>{calls.push({path,body});return {sources:[{...source(),family:'canalis-kt',asset_keys:[]}],has_more:true};});
await catalog.load({family:'canalis-kt'});
assert.equal(calls.length,1);assert.equal(calls[0].path,'/v1/industrial/sources:query');
assert.equal(calls[0].body.family,'canalis-kt');assert.match(summary.textContent,/切到全部资料分页查看/);
assert.equal(catalog.pager.hidden,true);
""")

    def test_raw_read_pins_document_version_and_preserves_exact_text(self):
        self.run_module(r"""
const calls=[];const {catalog}=make(async(path,body)=>{calls.push({path,body});return chunk(body.ordinal);});
await catalog.open(source(),1);
assert.equal(calls[0].path,'/v1/knowledge/sources:read');
assert.deepEqual(calls[0].body,{document_id:'doc',version_id:'v1',ordinal:1});
const content=catalog.dialog.children[1];const pre=content.children.find(n=>n.tagName==='pre');
assert.equal(pre.textContent,'泵体温度');
const nav=content.children.at(-1);assert.equal(nav.children[0].disabled,false);assert.equal(nav.children[1].disabled,true);
catalog.reset();assert.equal(catalog.dialog,null);
""")

    def test_source_cards_group_facts_without_replacing_titles_or_provenance(self):
        self.run_module(r"""
const calls=[],selected=[];
const {catalog}=make(async(path,body)=>{calls.push({path,body});return chunk(body.ordinal);},
  async(value,action)=>selected.push({value,action}));
const row={...source(),title:'authoritative_source',source_name:'local-controlled-upload',
  canonical_uri:'urn:local:authoritative_source.txt',asset_keys:['BC-P-101'],source_kind:'USER_UPLOAD',
  has_published_knowledge:true};
const before=JSON.stringify(row),card=catalog.renderCard(row);
const title=card.children.find(n=>n.className==='source-card-title');
assert.equal(title.textContent,row.title);
const summary=card.children.find(n=>n.className==='source-card-summary');
assert.match(summary.textContent,/第 1 版/);assert.match(summary.textContent,/2 个来源片段/);
assert.match(summary.textContent,/包含当前可见的已发布知识/);
assert.doesNotMatch(summary.textContent,/local-controlled-upload|authoritative_source/);
assert.match(card.children.find(n=>n.className==='source-card-header').textContent,/用户上传/);
assert.match(card.textContent,/BC-P-101/);
const details=card.children.find(n=>n.tagName==='details');
assert.equal(details.open,false);assert.match(details.textContent,/原始标题：authoritative_source/);
assert.match(details.textContent,/来源名称：local-controlled-upload/);
assert.match(details.textContent,/urn:local:authoritative_source.txt/);
assert.match(details.textContent,/文档 doc\n版本 v1/);
const actions=card.children.at(-1);
await actions.children[1].events.click();
assert.equal(selected[0].action,'graph');assert.deepEqual(selected[0].value,row);
await actions.children[0].events.click();
assert.deepEqual(calls[0].body,{document_id:'doc',version_id:'v1',ordinal:0});
assert.equal(JSON.stringify(row),before);
""")

    def test_source_cards_do_not_infer_publication_or_authority_from_filenames(self):
        self.run_module(r"""
const {catalog}=make(async()=>{});
const row={...source(),title:'authoritative_source',source_kind:null};
const unpublished=catalog.renderCard(row);
assert.match(unpublished.textContent,/尚无当前可见的已发布知识/);
assert.match(unpublished.children.find(n=>n.className==='source-card-header').textContent,/原始文档/);
assert.doesNotMatch(unpublished.textContent,/权威导入|官方发布/);
delete row.has_published_knowledge;
const unknown=catalog.renderCard(row);
assert.doesNotMatch(unknown.textContent,/当前可见的已发布知识/);
""")

    def test_version_mismatch_and_out_of_order_chunks_do_not_render(self):
        self.run_module(r"""
const first=deferred(),later=deferred();let reads=0;
const {catalog}=make(()=>++reads===1?first.promise:later.promise);
const opening=catalog.open(source(),0);const modal=catalog.dialog,content=modal.children[1];
const next=catalog.readChunk(source(),1,modal,content);
later.resolve(chunk(1));await next;first.resolve({...chunk(0),text:'stale content'});await opening;
assert.doesNotMatch(content.textContent,/stale content/);assert.match(content.textContent,/片段 2/);
const mismatch=make(async()=>chunk(0,'different-version')).catalog;
await mismatch.open(source());assert.match(mismatch.dialog.children[1].textContent,/版本或片段位置已变化/);
assert.doesNotMatch(mismatch.dialog.children[1].textContent,/泵体温度/);
""")
