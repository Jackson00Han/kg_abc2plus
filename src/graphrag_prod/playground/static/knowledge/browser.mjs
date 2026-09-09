import {mountSources} from './sources.mjs';
import {mountGraph} from './graph-view.mjs';
import {label,escape as e,valueLabel,contextLabel,entityPage,readDirectory,distinctionMarkup} from './model.mjs';
const $=id=>document.getElementById(id);
const origins={AUTHORITATIVE_EXTRACTED:'权威文档抽取',LLM_EXTRACTED:'业务文档抽取',HUMAN_SUPPLEMENT:'人工补充',EXPERT_IMPORT:'专家导入',EXPERT_CREATED:'专家建立',FIXTURE:'测试样例',RULE_DERIVED:'规则生成'};
export function evidenceMarkup(item) {
  const evidence=item.evidence,c=evidence.citation;
  const chars=Array.from(c.chunk_text),start=evidence.char_start-c.char_start,end=evidence.char_end-c.char_start;
  if(start<0 || end>chars.length || chars.slice(start,end).join('')!==evidence.quoted_text) throw new Error('原文位置校验失败。');
  return `<article><h3>${e(c.document_title || c.source_name || '来源文档')}</h3><p>${e(c.source_name)} · 第 ${e(c.version_number ?? "未记录")} 版 · 片段 ${e(c.ordinal ?? c.chunk_ordinal ?? '')} · 字符 ${e(evidence.char_start)}–${e(evidence.char_end)}</p><p class="kb-muted">${item.authority_level==='AUTHORITATIVE'?'权威来源':'业务来源'} · ${e(origins[item.origin]||item.origin)} · 已发布；${item.reviewed_by?`审核人：${e(item.reviewed_by)}`:'未记录人工审核人'}</p>${distinctionMarkup(item)}<pre>${e(chars.slice(0,start).join(''))}<mark>${e(chars.slice(start,end).join(''))}</mark>${e(chars.slice(end).join(''))}</pre><details><summary>技术详情</summary><pre>${e(JSON.stringify({revision_id:item.revision_id,record_id:item.record_id,version_id:c.version_id,provenance:evidence.provenance},null,2))}</pre></details></article>`;
}
export function mountBrowser({api,epoch,maintain}) {
  let directory=null,request=0,selected=null,page=0,active='entities',scope={},scopeTitle='',evidenceRequest=0,graph=null,sources=null;
  const dialog=document.createElement('dialog');dialog.className='kb-evidence';dialog.setAttribute('aria-label','来源依据');
  dialog.innerHTML='<button class="button" type="button" data-close>关闭依据</button><div data-content></div>';document.body.append(dialog);
  dialog.querySelector('[data-close]').onclick=()=>{evidenceRequest++;dialog.close();};
  dialog.addEventListener('cancel',()=>{evidenceRequest++;});
  function current(id,identity){return id===request && identity===epoch();}
  function reset(){scope={};scopeTitle='';$('kb-scope').textContent='';$('kb-clear-scope').hidden=true;request++;evidenceRequest++;directory=null;selected=null;page=0;graph?.clear();sources?.reset();dialog.close();dialog.querySelector('[data-content]').replaceChildren();$('kb-list').replaceChildren();$('kb-dossier').textContent='选择实体，查看属性、关系和来源。';$('kb-summary').textContent='请刷新知识，读取当前身份可访问的版本。';$('kb-search').value='';$('kb-type').innerHTML='<option value="">全部类型</option>';$('kb-prev').disabled=true;$('kb-next').disabled=true;$('kb-page').textContent='';}
  async function evidence(ids,token=directory?.view_token,offset=0){
    ids=[...new Set(ids)];
    if(!token || !ids.length) return;
    const id=++evidenceRequest,identity=epoch(),content=dialog.querySelector('[data-content]');
    content.textContent='正在授权读取原文依据…';if(!dialog.open)dialog.showModal();
    try{
      const result=await api('/v1/knowledge/graph:evidence',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({view_token:token,revision_ids:ids.slice(offset,offset+10)})});
      if(id!==evidenceRequest || identity!==epoch())return;
      content.innerHTML=result.items.length?result.items.map((item,index)=>evidenceMarkup(item)+`<button class="button" data-source="${index}">查看所在资料</button>`).join(''):'当前版本未找到可访问的依据，请刷新知识后重试。';
      content.insertAdjacentHTML('beforeend', `<p>来源记录 ${offset+1}–${Math.min(offset+10,ids.length)} / ${ids.length}</p><div class="kb-actions">${offset?'<button class="button" data-prev-evidence>上一页来源</button>':''}${offset+10<ids.length?'<button class="button" data-next-evidence>下一页来源</button>':''}</div>`);
      content.querySelector('[data-prev-evidence]')?.addEventListener('click',()=>evidence(ids,token,offset-10));
      content.querySelector('[data-next-evidence]')?.addEventListener('click',()=>evidence(ids,token,offset+10));
      content.querySelectorAll('[data-source]').forEach(b=>b.onclick=()=>{const c=result.items[Number(b.dataset.source)].evidence.citation;dialog.close();tab('sources');sources?.openDocument(c.document_id,c.version_id,c.ordinal);});
    }catch(error){if(id===evidenceRequest && identity===epoch())content.textContent=error.status===403?'当前身份没有查看该依据的权限。':error.status===409?'知识或来源权限已变化，请刷新知识。':`依据读取失败：${error.message}`;}
  }
  function select(entityId){
    const n=directory?.items.find(n=>n.entity_id===entityId);if(!n)return;
    selected=entityId;renderList();
    const property=n.properties.map(f=>`<div class="kb-fact"><strong>${e(label(f.predicate))}</strong>：${e(valueLabel(f))}${distinctionMarkup(f)}<div class="kb-muted">${e(contextLabel(f))} ${f.authority_level==='AUTHORITATIVE'?'权威来源':'业务来源'}</div><button class="button" data-evidence="${e(f.revision_id)}">查看依据</button></div>`).join('');
    const relations=n.relations.map((f,i)=>`<div class="kb-fact">${f.direction==='incoming'?'来自':'指向'} <button class="button" data-entity="${e(f.other.entity_id)}">${e(f.other.label)}</button> · ${e(label(f.predicate))}${distinctionMarkup(f)}<div class="kb-muted">${(f.relationship_properties||[]).map(p=>`${e(label(p.name))}：${e(valueLabel(p))} ${e(contextLabel(p))}`).join(" · ")}</div><button class="button" data-relation-evidence="${i}">查看 ${f.source_count||1} 条来源依据</button></div>`).join('');
    $('kb-dossier').innerHTML=`<h3>${e(n.label)}</h3><p class="kb-muted">${e(label(n.entity_type))}</p><h4>属性 · ${n.properties.length}</h4>${property||'<p>当前范围暂无已发布属性。</p>'}<h4>关联关系 · ${n.relations.length}</h4>${relations||'<p>当前范围尚未发布实体关系，可核对来源或在构建流程补充。</p>'}<h4>原文提及 · ${n.mention_revision_ids.length}</h4><div class="kb-actions">${n.mention_revision_ids.map((id,i)=>`<button class="button" data-evidence="${e(id)}">来源位置 ${i+1}</button>`).join('')}</div><div class="kb-actions" style="margin-top:18px"><button class="button" data-maintain>发起维护</button><button class="button" data-locate>在图中查看</button></div><details><summary>技术详情</summary><p>实体 ID：${e(n.entity_id)}</p><p>身份键：${e(n.canonical_key)}</p></details>`;
    $('kb-dossier').querySelectorAll('[data-evidence]').forEach(b=>b.onclick=()=>evidence([b.dataset.evidence]));
    $('kb-dossier').querySelectorAll('[data-relation-evidence]').forEach(b=>b.onclick=()=>{const f=n.relations[Number(b.dataset.relationEvidence)];evidence(f.revision_ids||[f.revision_id]);});
    $('kb-dossier').querySelectorAll('[data-entity]').forEach(b=>b.onclick=()=>select(b.dataset.entity));
    $('kb-dossier').querySelector('[data-maintain]').onclick=()=>maintain(n);
    $('kb-dossier').querySelector('[data-locate]').onclick=()=>{tab('graph');graph?.locate(n.entity_id);};
  }
  function renderList(){
    if(!directory)return;
    const result=entityPage(directory.items,{query:$('kb-search').value,type:$('kb-type').value,page});page=result.page;
    $('kb-list').innerHTML=result.items.map(n=>`<button class="kb-entity" aria-pressed="${n.entity_id===selected}" data-id="${e(n.entity_id)}"><strong>${e(n.label)}</strong><div class="kb-muted">${e(label(n.entity_type))} · ${n.properties.length} 条属性 · ${n.relations.length} 条关系</div><div>${e(n.properties.slice(0,2).map(f=>`${label(f.predicate)}：${valueLabel(f)}`).join(' · '))}</div></button>`).join('')||'<p>当前筛选下没有可访问的已发布实体。</p>';
    $('kb-list').querySelectorAll('[data-id]').forEach(b=>b.onclick=()=>select(b.dataset.id));
    $('kb-page').textContent=`符合条件 ${result.total} 个实体 · 第 ${result.pages?result.page+1:0} / ${result.pages} 页`;
    $('kb-prev').disabled=result.page===0;$('kb-next').disabled=result.page+1>=result.pages;
  }
  async function load(version_filter=scope,title=scopeTitle){
    scope=version_filter;scopeTitle=title;$('kb-clear-scope').hidden=active==='sources'||!Object.keys(scope).length;$('kb-scope').textContent=Object.keys(scope).length?`来源范围：${title||'指定资料及版本'}`:'范围：当前身份可访问的已发布知识';
    const id=++request,identity=epoch();directory=null;selected=null;page=0;graph?.clear();evidenceRequest++;dialog.close();
    $('kb-list').replaceChildren();$('kb-dossier').textContent='正在读取完整实体资料…';$('kb-summary').textContent='正在读取当前授权知识范围…';
    try{
      const result=await readDirectory(api,version_filter,()=>current(id,identity));if(!current(id,identity))return;
      directory=result;
      $('kb-type').innerHTML='<option value="">全部类型</option>'+[...new Set(result.items.map(n=>n.entity_type))].sort().map(t=>`<option value="${e(t)}">${e(label(t))}</option>`).join('');
      const properties=result.items.reduce((sum,n)=>sum+n.properties.length,0),edges=new Set(result.items.flatMap(n=>n.relations.map(f=>f.fact_key||f.revision_id))).size;
      $('kb-summary').textContent=`${result.pin.publication_id?`本次读取：第 ${result.pin.publication_generation} 版`:'当前尚无生效发布'} · 当前授权${Object.keys(version_filter).length?'筛选':''}范围：${result.items.length} 个实体 · ${properties} 条属性 · ${edges} 条关系`;
      renderList();$('kb-dossier').textContent='选择实体，查看属性、关系和来源。';graph?.setDirectory(result);
    }catch(error){if(current(id,identity)){$('kb-summary').textContent=error.status===403?'当前身份没有知识浏览权限。':`知识读取失败：${error.message}。可按来源缩小范围后重试。`;$('kb-dossier').textContent='未显示不完整或过期的知识。';}}
  }
  function tab(name){active=name;$('kb-filters').hidden=name==='sources';$('kb-clear-scope').hidden=name==='sources'||!Object.keys(scope).length;$('kb-scope').hidden=name==='sources';$('kb-refresh').hidden=name==='sources';const home=$('kb-detail-home'),graphHome=$('kb-detail-graph');if(home && graphHome)(name==='graph'?graphHome:home).appendChild($('kb-dossier'));for(const view of ['entities','graph','sources']){$(`kb-${view}`).hidden=name!==view;$(`kb-tab-${view}`).setAttribute('aria-selected',String(name===view));}if(name==='graph')graph?.activate();if(name==='sources')sources?.activate();}
  for(const view of ['entities','graph','sources'])$(`kb-tab-${view}`).onclick=()=>tab(view);
  $('kb-refresh').onclick=()=>load();$('kb-clear-scope').onclick=()=>load({},'');$('kb-search').oninput=()=>{page=0;renderList();};$('kb-type').onchange=()=>{page=0;renderList();};
  $('kb-prev').onclick=()=>{page--;renderList();};$('kb-next').onclick=()=>{page++;renderList();};
  const controller = {activate(){if(active==='sources')sources?.activate();if(!directory)return load();if(active==='graph')graph?.activate();},reset,load,evidence,select,tab,getDirectory:()=>directory,
    attachGraph(value){graph=value;if(directory)graph.setDirectory(directory);if(active==='graph')graph.activate();},
    attachSources(value){sources=value;if(active==='sources')sources.activate();}};
  graph=mountGraph({api,epoch,browser:controller});
  sources=mountSources({api,epoch,browser:controller,maintain});
  return controller;
}
