/** Version-bound loading is independent of Cytoscape, layout and DOM. */
export class GraphSession {
  constructor(api,epoch){this.api=api;this.epoch=epoch;this.serial=0;this.page=null;this.query=null;}
  clear(){this.serial++;this.page=null;this.query=null;}
  async read(query,{token=null,cursor=null}={}){
    const serial=++this.serial,identity=this.epoch();this.page=null;
    const response=await this.api('/v1/knowledge/graph:query',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...query,...(token?{view_token:token}:{}),...(cursor?{cursor}:{})})});
    if(serial!==this.serial || identity!==this.epoch())return null;
    if(token && response.view_token!==token)throw new Error('知识版本发生变化，请刷新知识。');
    this.page=response;this.query=query;return response;
  }
  next(){if(!this.page?.page.has_more)return Promise.resolve(null);return this.read(this.query,{token:this.page.view_token,cursor:this.page.page.next_cursor});}
}
import {label,escape as e,entityPage} from './model.mjs';
const $=id=>document.getElementById(id);
export function mountGraph({api,epoch,browser,feedback=()=>()=>{}}) {
  const session=new GraphSession(api,epoch);let renderer=null,creating=null,directory=null,seed=null,uiRequest=0;
  $('kb-graph').innerHTML=`<div class="kb-actions"><button class="button" id="kb-graph-load">按当前筛选查看</button><label>探索深度 <select id="kb-hops"><option value="1">一层关系</option><option value="2">两层关系</option></select></label><label>方向 <select id="kb-direction"><option value="both">全部方向</option><option value="outgoing">指向其他实体</option><option value="incoming">来自其他实体</option></select></label><label>关系 <select id="kb-predicate"><option value="">全部关系</option></select></label><button class="button" id="kb-graph-next" disabled>下一组</button></div><p id="kb-graph-status" role="status">先加载知识，再探索图谱。</p><div class="kb-workspace"><div><div class="kb-graph-canvas" id="kb-graph-canvas" aria-label="已发布关系图谱"></div><div class="kb-actions"><button class="button" id="kb-zoom-in">放大</button><button class="button" id="kb-zoom-out">缩小</button><button class="button" id="kb-fit">适应画布</button><button class="button" id="kb-reset-graph">恢复范围</button></div><p class="kb-muted">箭头表示已发布关系方向；同一关系聚合为一条线；实线表示含权威来源，虚线表示仅含业务来源，各来源等级分别保留。布局位置不表示工艺顺序或因果关系。</p><div id="kb-graph-accessible"></div></div><div id="kb-detail-graph"></div></div>`;
  async function ensureRenderer(){
    if(renderer)return renderer;
    if(!creating)creating=import('/industrial/assets/graph.mjs').then(({IndustrialGraph})=>{
      renderer=new IndustrialGraph($('kb-graph-canvas'),{onSelect(item){
        if(item?.kind==='node')browser.select(item.entity.entity_id);
        if(item?.kind==='edge')browser.evidence(item.assertion.revision_ids||[item.assertion.revision_id],session.page?.view_token);
      },layoutOptions:{rankDir:'LR'},transformElements(items){return items.map(item=>({...item,data:{...item.data,...(item.data.entity?{typeLabel:label(item.data.entity.entity_type),displayLabel:`${item.data.entity.label}\n${label(item.data.entity.entity_type)}`}:{label:label(item.data.assertion.predicate)})}}));}});
      return renderer;
    }).finally(()=>{creating=null;});
    return creating;
  }
  function clear(){uiRequest++;session.clear();directory=null;seed=null;renderer?.clear();$('kb-graph-status').textContent='知识范围已变化，请重新加载。';$('kb-graph-accessible').replaceChildren();$('kb-graph-next').disabled=true;}
  async function draw(page,id){
    if(!page || id!==uiRequest)return;
    const identity=epoch(),view=await ensureRenderer();if(id!==uiRequest || identity!==epoch())return;
    view.setPage(page,'all');
    $('kb-graph-status').textContent=`当前展示 ${page.nodes.length} 个实体 · ${page.edges.length} 条关系${page.edges.length?'':'；当前展示范围没有已发布实体关系'}${page.page.has_more?' · 还有内容未展开，可查看下一组':' · 当前查询已全部展示'}。`;
    $('kb-graph-next').disabled=!page.page.has_more;
    const nodes=new Map(page.nodes.map(n=>[n.entity_id,n.label]));
    $('kb-graph-accessible').innerHTML=`<details><summary>图谱内容列表（可使用键盘操作）</summary>${page.nodes.map(n=>`<button class="button" data-node="${e(n.entity_id)}">${e(n.label)}</button>`).join('')}${page.edges.map((f,i)=>`<p>${e(nodes.get(f.source))} → ${e(label(f.predicate))} → ${e(nodes.get(f.target))} <button class="button" data-edge="${i}">查看 ${f.source_count||1} 条来源</button></p>`).join('')}</details>`;
    $('kb-graph-accessible').querySelectorAll('[data-node]').forEach(b=>b.onclick=()=>browser.select(b.dataset.node));
    $('kb-graph-accessible').querySelectorAll('[data-edge]').forEach(b=>b.onclick=()=>{const f=page.edges[Number(b.dataset.edge)];browser.evidence(f.revision_ids||[f.revision_id],page.view_token);});
  }
  async function load(next=false){
    if(!directory){$('kb-graph-status').textContent='请先刷新知识，或在来源资料中缩小范围。';return;}
    const finish=feedback($(next?'kb-graph-next':'kb-graph-load'));
    const id=++uiRequest,identity=epoch();renderer?.clear();$('kb-graph-next').disabled=true;$('kb-graph-status').textContent='';
    try{
      let page;
      if(next)page=await session.next();
      else {
        const q=$('kb-search').value.trim(),type=$('kb-type').value;
        let seeds=seed?[seed]:[];
        if(!seed && q){const matches=entityPage(directory.items,{query:q,type,size:9}).items;if(matches.length>8)throw new Error('匹配实体较多，请缩小名称或编号搜索范围。');seeds=matches.map(n=>n.entity_id);if(!seeds.length){session.clear();$('kb-graph-accessible').replaceChildren();$('kb-graph-status').textContent='当前筛选没有匹配实体。';return;}}
        const predicate=$('kb-predicate').value;
        page=await session.read({page_size:100,version_filter:directory.version_filter,seed_entity_ids:seeds,entity_types:seed?[]:type?[type]:[],predicates:predicate?[predicate]:[],hops:Number($('kb-hops').value),direction:$('kb-direction').value},{token:directory.view_token});
      }
      await draw(page,id);
    }catch(error){if(id===uiRequest && identity===epoch()){$('kb-graph-status').textContent=error.status===409?'知识版本或权限已变化，请刷新知识后重试。':`图谱读取失败：${error.message}`;$('kb-graph-accessible').replaceChildren();}}finally{finish();if(id===uiRequest)$('kb-graph-next').disabled=!session.page?.page.has_more;}
  }
  $('kb-graph-load').onclick=()=>{seed=null;load();};$('kb-graph-next').onclick=()=>load(true);
  $('kb-hops').onchange=()=>load();$('kb-direction').onchange=()=>load();$('kb-predicate').onchange=()=>load();
  $('kb-zoom-in').onclick=()=>renderer?.zoom(1.25);$('kb-zoom-out').onclick=()=>renderer?.zoom(.8);$('kb-fit').onclick=()=>renderer?.fit();
  $('kb-reset-graph').onclick=()=>{seed=null;$('kb-search').value='';$('kb-type').value='';$('kb-predicate').value='';$('kb-hops').value='1';$('kb-direction').value='both';load();};
  return {clear,setDirectory(value){directory=value;$('kb-predicate').innerHTML='<option value="">全部关系</option>'+value.schema.relationship_types.map(t=>`<option value="${e(t.name)}">${e(label(t.name))}</option>`).join('');if(!$('kb-graph').hidden)load();},activate(){if(!session.page && directory)load();else renderer?.fit();},locate(id){seed=id;load();}};
}
