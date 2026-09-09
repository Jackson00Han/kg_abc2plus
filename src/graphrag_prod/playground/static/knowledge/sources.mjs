import {escape as e} from './model.mjs';
const $=id=>document.getElementById(id);
export function mountSources({api,epoch,browser,maintain}){
  let request=0,detailRequest=0,loaded=false,after=null,history=[],next=null,current=null,rows=[];
  $('kb-sources').innerHTML='<div class="kb-actions"><button class="button" id="kb-source-refresh">刷新资料</button><button class="button" id="kb-source-prev" disabled>上一页</button><button class="button" id="kb-source-next" disabled>下一页</button></div><p id="kb-source-summary" role="status">尚未加载来源资料。</p><div class="kb-workspace"><div id="kb-source-list"></div><aside class="kb-dossier" id="kb-source-detail">选择资料，查看原文与相关知识。</aside></div>';
  function reset(){request++;detailRequest++;loaded=false;after=null;history=[];next=null;current=null;rows=[];$('kb-source-list').replaceChildren();$('kb-source-detail').textContent='选择资料，查看原文与相关知识。';$('kb-source-summary').textContent='尚未加载当前身份的资料。';$('kb-source-prev').disabled=true;$('kb-source-next').disabled=true;}
  async function load(){
    const id=++request,identity=epoch();$('kb-source-list').replaceChildren();$('kb-source-summary').textContent='正在读取当前可访问的资料…';
    try{const result=await api('/v1/knowledge/sources:query',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({limit:30,...(after?{after}:{})})});if(id!==request || identity!==epoch())return;
      loaded=true;rows=result.items;next=result.next_after;
      $('kb-source-summary').textContent=`本页 ${rows.length} 份可访问资料${result.has_more?' · 还有更多资料':' · 已到列表末尾'}。已入库不等于知识已发布。`;
      $('kb-source-prev').disabled=!history.length;$('kb-source-next').disabled=!result.has_more;
      $('kb-source-list').innerHTML=rows.map((r,i)=>`<article class="kb-source"><h3>${e(r.title)}</h3><p class="kb-muted">${e(r.source_name)} · 第 ${r.version_number} 版 · ${r.chunk_count} 个片段</p><p>${r.has_published_knowledge?'包含当前可访问的已发布知识':'尚未发现当前可访问的已发布知识'}</p><div class="kb-actions"><button class="button" data-open="${i}">查看原文</button><button class="button" data-knowledge="${i}">查看相关知识</button></div></article>`).join('')||'<p>当前身份没有可访问的活动资料。</p>';
      $('kb-source-list').querySelectorAll('[data-open]').forEach(b=>b.onclick=()=>{const r=rows[Number(b.dataset.open)];openDocument(r.document_id,r.version_id);});
      $('kb-source-list').querySelectorAll('[data-knowledge]').forEach(b=>b.onclick=()=>knowledge(rows[Number(b.dataset.knowledge)]));
    }catch(error){if(id===request && identity===epoch()){$('kb-source-summary').textContent=`资料读取失败：${error.message}`;$('kb-source-next').disabled=true;}}
  }
  async function knowledge(r){browser.tab('entities');await browser.load({document_ids:[r.document_id],version_ids:[r.version_id]});}
  async function openDocument(document_id,version_id,ordinal=0){
    const id=++detailRequest,identity=epoch();current=null;$('kb-source-detail').textContent='正在授权读取指定版本的原文…';
    try{const r=await api('/v1/knowledge/sources:read',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({document_id,version_id,ordinal})});if(id!==detailRequest || identity!==epoch())return;current=r;
      $('kb-source-detail').innerHTML=`<h3>${e(r.title)}</h3><p class="kb-muted">第 ${r.version_number} 版 · 片段 ${r.ordinal+1}/${r.chunk_count} · 字符 ${r.char_start}–${r.char_end}${r.page_number?` · 第 ${r.page_number} 页`:''}</p><pre class="kb-source-text">${e(r.text)}</pre><div class="kb-actions"><button class="button" data-before ${r.ordinal===0?'disabled':''}>上一片段</button><button class="button" data-after ${r.ordinal+1>=r.chunk_count?'disabled':''}>下一片段</button><button class="button" data-facts>查看相关知识</button><button class="button" data-maintain>资料维护</button></div><details><summary>来源与版本详情</summary><p>${e(r.canonical_uri)}</p><p>版本：${e(r.version_id)}</p><p>校验：${e(r.checksum)}</p></details>`;
      $('kb-source-detail').querySelector('[data-before]').onclick=()=>openDocument(document_id,version_id,ordinal-1);$('kb-source-detail').querySelector('[data-after]').onclick=()=>openDocument(document_id,version_id,ordinal+1);$('kb-source-detail').querySelector('[data-facts]').onclick=()=>knowledge(r);$('kb-source-detail').querySelector('[data-maintain]').onclick=()=>maintain(r);
    }catch(error){if(id===detailRequest && identity===epoch())$('kb-source-detail').textContent=error.status===409?'来源版本、完整性或访问权限已变化，请刷新资料列表。':`原文读取失败：${error.message}`;}
  }
  $('kb-source-refresh').onclick=()=>{reset();load();};$('kb-source-next').onclick=()=>{history.push(after);after=next;load();};$('kb-source-prev').onclick=()=>{after=history.pop()??null;load();};
  return {activate(){if(!loaded)load();},reset,openDocument};
}
