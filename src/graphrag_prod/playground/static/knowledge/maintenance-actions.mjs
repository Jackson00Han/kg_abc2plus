/** Contextual maintenance reuses the guarded review and publication workflows. */
import {escape as e,label,valueLabel,contextLabel} from './model.mjs';
export function factTitle(item){
  if(item.entity)return `${item.entity.display_name} · 原文提及`;
  if(item.assertion){const a=item.assertion;return `${a.subject?.display_name||'实体'} · ${label(a.predicate)} · ${a.object_entity?.display_name||valueLabel({value:a.literal?.value,semantics:a.literal})}`;}
  return `${item.subject_name} · ${item.record_kind==='ENTITY_MENTION'?'原文提及':`${label(item.predicate)} · ${item.object_name||valueLabel({value:item.literal_value,semantics:{raw_unit:item.unit}})}`}`;
}
export function comparisonMarkup(result){
  const row=item=>`<li><strong>${e(factTitle(item))}</strong><p>${e(item.document_title)} ${e([item.valid_from,item.valid_to,item.observed_at].filter(Boolean).join(' · '))}</p></li>`;
  return `<p>切换后新增 ${result.added.length} 条、移除 ${result.removed.length} 条、变更 ${result.changed.length} 条；保持 ${result.unchanged_count} 条。</p><h4>新增</h4><ul>${result.added.map(row).join('')||'<li>无</li>'}</ul><h4>移除</h4><ul>${result.removed.map(row).join('')||'<li>无</li>'}</ul><h4>变更</h4><ul>${result.changed.map(c=>`<li><p>当前：${e(factTitle(c.before))}</p><p>目标：${e(factTitle(c.after))}</p><p>来源：${e(c.before.document_title)} → ${e(c.after.document_title)}</p><small>内容、来源依据或记录版本发生变化。</small></li>`).join('')||'<li>无</li>'}</ul>`;
}
export function mountActions({api,epoch,browser,navigate,correctRecord,rollback,toast,maintainSource}){
  let serial=0;
  const dialog=document.createElement('dialog');dialog.className='kb-evidence';dialog.setAttribute('aria-label','知识维护');document.body.append(dialog);
  function reset(){serial++;dialog.close();dialog.replaceChildren();}
  dialog.addEventListener('cancel',reset);
  function open(title,content){reset();dialog.innerHTML=`<h3>${e(title)}</h3>${content}<p data-status role="status"></p><div class="kb-actions"><button class="button" data-close>关闭</button></div>`;dialog.querySelector('[data-close]').onclick=reset;dialog.showModal();return {id:serial,identity:epoch()};}
  const valid=pin=>pin.id===serial && pin.identity===epoch();
  function errorText(error){return error.status===403?'当前身份没有完整查看或维护这些知识的权限。':error.status===409?'版本、来源或权限已变化，或内容超出比较范围。请刷新后重试。':error.message;}
  async function correct(recordId,revisionId=null){
    const pin={id:serial,identity:epoch()};const status=dialog.querySelector('[data-status]');
    try{if(status)status.textContent='正在定位可编辑记录…';await correctRecord(recordId,revisionId);if(valid(pin))reset();}
    catch(error){if(valid(pin) && status)status.textContent=errorText(error);else if(pin.identity===epoch())toast(errorText(error));}
  }
  async function maintain(item){
    if(item.document_id){await maintainSource(item);return;}
    if(item.object_id){
      const d=browser.getDirectory();const n=d?.items.find(n=>n.entity_id===item.object_id || n.mention_revision_ids.includes(item.object_id) || [...n.properties,...n.relations].some(f=>f.revision_id===item.object_id || f.record_id===item.object_id));
      if(n)return maintain(n);
      open('定位待修正知识','<p>当前浏览视图中无法定位该对象。可以尝试按记录读取；图谱结构问题需根据质量报告修复来源关联或知识模型。</p>');
      return correct(item.object_id);
    }
    const facts=[...new Map([...item.properties,...item.relations].map(f=>[f.revision_id,f])).values()];
    const choices=[...facts.map(f=>({revision_id:f.revision_id,title:`${label(f.predicate)}：${f.other?.label||valueLabel(f)}`,record_id:f.record_id})),...item.mention_revision_ids.map((id,i)=>({revision_id:id,title:`原文提及 ${i+1}`}))];
    const pin=open(`${item.label} · 选择要修正的内容`,`<p>开始修正会生成待修订记录，进入现有审核流程。当前检查可能提示记录版本不一致；完成审核并重新发布后恢复一致。</p>${choices.map((f,i)=>`<article class="kb-source"><strong>${e(f.title)}</strong><div class="kb-actions"><button class="button" data-proof="${i}">核对依据</button><button class="button" data-correct="${i}">进入修正</button></div></article>`).join('')}`);
    const token=browser.getDirectory()?.view_token;
    dialog.querySelectorAll('[data-proof]').forEach(b=>b.onclick=()=>browser.evidence([choices[Number(b.dataset.proof)].revision_id],token));
    dialog.querySelectorAll('[data-correct]').forEach(b=>b.onclick=async()=>{
      b.disabled=true;const choice=choices[Number(b.dataset.correct)];
      try{
        let id=choice.record_id;
        if(!id){const result=await api('/v1/knowledge/graph:evidence',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({view_token:token,revision_ids:[choice.revision_id]})});if(!valid(pin))return;id=result.items[0]?.record_id;}
        if(!id)throw new Error('未找到可访问的记录，请刷新知识。');
        await correct(id,choice.revision_id);
      }catch(error){if(valid(pin))dialog.querySelector('[data-status]').textContent=errorText(error);}
      finally{if(valid(pin))b.disabled=false;}
    });
  }
  function inventory(inventory,container,summary,selected,onSelect,onHistory){
    summary.textContent=`当前生效：第 ${inventory.publication_generation} 版 · 当前筛选 ${inventory.matching_record_count} 条 / 已发布 ${inventory.total_record_count} 条${inventory.truncated?' · 本页达到上限，请按来源缩小范围':''}`;
    container.innerHTML=(inventory.items||[]).map((item,index)=>`<article class="kb-source"><label class="checkbox-label"><input type="checkbox" data-select="${index}" ${selected.has(item.revision_id)?'checked':''}><strong>${e(factTitle(item))}</strong></label><p>${item.authority_level==='AUTHORITATIVE'?'权威来源':'业务来源'} · 已发布 ${e(contextLabel({semantics:item.assertion?.literal}))}</p><p>来源片段 ${e(item.evidence?.ordinal)} · 字符 ${e(item.evidence?.char_start)}–${e(item.evidence?.char_end)}</p><div class="kb-actions"><button class="button" data-correct-record="${index}">修正这条知识</button><button class="button" data-history="${index}">查看修订历史</button></div><details><summary>技术详情与证据位置</summary><pre>${e(JSON.stringify({record_id:item.record_id,revision_id:item.revision_id,evidence:item.evidence,origin:item.origin},null,2))}</pre></details></article>`).join('')||'<p>当前筛选下没有已发布知识。</p>';
    container.querySelectorAll('[data-select]').forEach(b=>b.onchange=()=>onSelect(inventory.items[Number(b.dataset.select)],b.checked));
    container.querySelectorAll('[data-correct-record]').forEach(b=>b.onclick=()=>{open('修正已发布知识',`<p>${e(factTitle(inventory.items[Number(b.dataset.correctRecord)]))}</p><p>开始后生成待修订记录，当前检查可能提示版本不一致。完成审核并重新发布后恢复一致。</p><button class="button primary" data-begin>开始修正</button>`);dialog.querySelector('[data-begin]').onclick=()=>correct(inventory.items[Number(b.dataset.correctRecord)].record_id,inventory.items[Number(b.dataset.correctRecord)].revision_id);});
    container.querySelectorAll('[data-history]').forEach(b=>b.onclick=()=>revisionHistory(inventory.items[Number(b.dataset.history)]));
  }
  async function revisionHistory(item){
    const pin=open('知识修订历史',`<p>${e(factTitle(item))}</p><div data-history-content>正在读取…</div>`);
    try{const result=await api(`/v1/knowledge/records/${encodeURIComponent(item.record_id)}/revisions?limit=100`);if(!valid(pin))return;
      dialog.querySelector('[data-history-content]').innerHTML=result.items.map(r=>`<article class="kb-source"><strong>第 ${r.revision} 次修订 · ${e({PUBLISHED:'已发布',APPROVED:'审核通过',QUARANTINED:'待修正',CANDIDATE:'待审核',REJECTED:'已拒绝'}[r.trust.status]||r.trust.status)}</strong><p>${e(r.trust.reviewed_by||'未记录审核人')} · ${e(r.trust.reviewed_at||'未审核')}</p><details><summary>技术详情</summary><p>${e(r.revision_id)}</p></details></article>`).join('')+(result.items.length===100?'<p>仅显示最近 100 次修订。</p>':'');
    }catch(error){if(valid(pin))dialog.querySelector('[data-status]').textContent=errorText(error);}
  }
  function removals(items,commit,isCurrent){
    const pin=open('确认移除范围',`<p>将从下一发布版本移除以下 ${items.length} 条记录。来源资料仍保留；相关实体与关系依赖由发布预览统一校验。</p><ul>${items.map(i=>`<li>${e(factTitle(i))}</li>`).join('')}</ul><p>此处仅加入待发布清单，下一步查看完整发布影响。</p><button class="button primary" data-stage>加入清单并预览影响</button>`);
    dialog.querySelector('[data-stage]').onclick=()=>{if(!valid(pin)||!isCurrent()){dialog.querySelector('[data-status]').textContent='清单已变化，请重新选择。';return;}reset();commit();};
  }
  function history(items,container){
    const active=items.find(p=>p.status==='ACTIVE');
    const targets=active?items.filter(p=>p.publication_id!==active.publication_id):[];
    const reason=!items.length?'尚未发布知识。首次发布后会生成版本记录；再次发布后，可以回滚到之前的版本。':!active?'当前没有可访问的生效版本，暂时无法回滚。请核对当前身份权限与发布状态。':!targets.length?'当前仅有一个可访问的发布版本，暂无历史版本可回滚。后续发布新版本后，可在这里选择之前的版本。':'选择历史版本，先查看新增、移除和变更的知识，再确认回滚。回滚会重新启用目标版本，记录本次切换并保留完整发布历史。';
    container.innerHTML=`<div class="kb-source"><h3>版本回滚</h3><p>${active?`当前生效：第 ${e(active.generation)} 版。`:''}${e(reason)}</p><div class="kb-actions"><label>目标版本 <select data-rollback-target aria-label="选择回滚目标版本" ${targets.length?'':'disabled'}><option value="">${targets.length?'请选择历史版本':'暂无可回滚版本'}</option>${targets.map(p=>`<option value="${e(p.publication_id)}">第 ${e(p.generation)} 版 · ${e(new Date(p.created_at).toLocaleString('zh-CN'))}</option>`).join('')}</select></label><button class="button" data-open-rollback disabled>查看影响并回滚</button></div></div>`+items.map((p,index)=>`<article class="kb-source"><h3>第 ${e(p.generation)} 版 · ${p.status==='ACTIVE'?'当前生效':'历史版本'}</h3><p>${e(new Date(p.created_at).toLocaleString('zh-CN'))} · 发布人 ${e(p.created_by)} · ${p.published_revision_ids.length} 条知识记录</p>${active&&p.publication_id!==active.publication_id?`<button class="button" data-compare="${index}">回滚到此版本…</button>`:''}<details><summary>版本技术详情</summary><p>${e(p.publication_id)}</p><p>知识模型版本：${e(p.ontology_version_id)}</p></details></article>`).join('');
    const select=container.querySelector('[data-rollback-target]');
    const button=container.querySelector('[data-open-rollback]');
    select.onchange=()=>{button.disabled=!targets.some(p=>p.publication_id===select.value);};
    button.onclick=()=>{const target=targets.find(p=>p.publication_id===select.value);if(target&&active)return compare(target,active);};
    if(items.length===100)container.insertAdjacentHTML('beforeend','<p>仅显示最近 100 个可访问版本。</p>');
    container.querySelectorAll('[data-compare]').forEach(b=>b.onclick=()=>compare(items[Number(b.dataset.compare)],active));
  }
  async function compare(target,active){
    const pin=open(`回滚到第 ${target.generation} 版前的影响`,`<p>正在比较当前第 ${active.generation} 版与目标版本…</p><div data-comparison></div>`);
    try{const result=await api('/v1/knowledge/publications:compare',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target_publication_id:target.publication_id,expected_active_publication_id:active.publication_id})});if(!valid(pin))return;
      dialog.querySelector('[data-comparison]').innerHTML=`${comparisonMarkup(result)}<p>确认后将重新启用目标版本的知识内容，记录本次切换并保留此前发布历史。此操作不会恢复已撤回的来源资料，也不会切换知识模型；来源与模型不兼容时，服务器会阻止回滚。</p><button class="button danger" data-confirm>确认回滚到第 ${target.generation} 版</button>`;
      dialog.querySelector('[data-confirm]').onclick=async()=>{if(!valid(pin))return;const b=dialog.querySelector('[data-confirm]');if(b.disabled)return;b.disabled=true;b.textContent='正在回滚…';try{await rollback(target.publication_id,active.publication_id);if(valid(pin))reset();}catch(error){if(valid(pin)){dialog.querySelector('[data-status]').textContent=errorText(error);b.textContent='回滚未完成，请关闭后重新比较';}}};
    }catch(error){if(valid(pin))dialog.querySelector('[data-status]').textContent=errorText(error);}
  }
  return {reset,maintain,inventory,history,removals};
}
