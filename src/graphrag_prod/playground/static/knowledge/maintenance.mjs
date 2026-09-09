/** Readable audit presentation. Automatic findings and human conclusions stay separate. */
import {escape as e,label} from './model.mjs';
export const ISSUE_LABELS=Object.freeze({ISOLATED_ENTITY:'尚无属性或关系的实体',ORPHAN_ENTITY:'实体缺少已发布来源记录',DUPLICATE_ENTITY:'实体身份重复',ANOMALOUS_HUB:'实体连接数量异常',ACTIVE_ENTITY_MEMBERSHIP_MISSING:'实体未进入当前图谱快照',RELATIONSHIP_PATTERN_INVALID:'关系不符合知识模型',RELATIONSHIP_ENDPOINT_CARDINALITY_INVALID:'关系端点数量异常',TYPED_LITERAL_INVALID:'属性数值、单位或日期格式异常',EVIDENCE_ACTIVE_SNAPSHOT_INVALID:'来源版本或证据位置异常',HEAD_CURRENT_REVISION_INVALID:'审核记录的当前版本异常',INVALID_REVISION_LABEL:'记录类型标记异常',PUBLICATION_RECORD_KIND_INVALID:'发布记录类型异常',REVISION_STATUS_INVALID:'发布记录状态异常',REVISION_TBOX_MISMATCH:'记录引用的知识模型不一致',REVISION_TENANT_MISMATCH:'记录所属知识库不一致',ORIGIN_AUTHORITY_INVALID:'来源等级与形成方式不一致',ENTITY_TYPE_UNDECLARED:'实体类型未在知识模型中定义',ENTITY_NAMESPACE_INVALID:'实体身份格式不符合知识模型',CANONICAL_ENTITY_SCHEMA_INVALID:'实体身份或类型不符合约束',ASSERTION_SUBJECT_SCHEMA_INVALID:'事实主体不符合知识模型',ASSERTION_OBJECT_KIND_INVALID:'事实的对象类型异常',MENTION_ENTITY_LINK_INVALID:'原文提及与实体关联异常',SUBJECT_MENTION_LINK_INVALID:'事实主体缺少有效来源关联',OBJECT_MENTION_LINK_INVALID:'关系对象缺少有效来源关联',RELATIONSHIP_PROPERTIES_INVALID:'关系附加属性异常',RELATIONSHIP_PROPERTY_SCHEMA_INVALID:'关系属性不符合知识模型',RELATIONSHIP_PROPERTY_MATERIALIZATION_INVALID:'关系属性与当前图谱不一致',ACTIVE_ASSERTION_MATERIALIZATION_INVALID:'事实记录与当前图谱不一致',ACTIVE_ASSERTION_PROJECTION_INVALID:'事实在当前图谱中的映射异常',ACTIVE_MENTION_MATERIALIZATION_INVALID:'来源提及与当前图谱不一致',ACTIVE_MENTION_PROJECTION_INVALID:'来源提及在当前图谱中的映射异常'});
const DECISIONS={PENDING:'待核查',NEEDS_CORRECTION:'确认需修正',NO_CHANGE_REQUIRED:'已核查，无需修正'};
export function qualityMarkup(report,{labels={},records=[],historical=false}={}){
  const counts=report.counts||{},latest=new Map();for(const row of records)if(!latest.has(row.issue_id))latest.set(row.issue_id,row);
  const issues=(report.issues||[]).map((item,index)=>{
    const title=labels[item.object_id] || ({Entity:'实体',Assertion:'事实',EntityMention:'原文提及'}[item.object_kind]||'知识对象');
    const review=latest.get(item.issue_id);
    return `<article class="kb-source"><h3>${e(title)} · ${e(ISSUE_LABELS[item.code]||'知识一致性问题')}</h3><p>${item.severity==='ERROR'?'需要修正':item.severity==='REVIEW'?'建议人工核查':'提醒'}</p><p>${item.code==='ISOLATED_ENTITY'?'该实体未参与任何已发布属性或关系。请核对来源是否遗漏事实；原文没有支持的事实时可以保留。':'请核对对象、来源依据和知识模型，确认原因后发起修正。'}</p><p>${review?`${e(DECISIONS[review.decision])} · ${e(review.recorded_by)} · ${e(new Date(review.recorded_at).toLocaleString('zh-CN'))}<br>${e(review.notes)}`:'尚未加载到本项的人工核查结论。'}</p><div class="kb-actions">${historical?'':`<button class="button" data-quality-object="${index}">查看对象与依据</button><button class="button" data-quality-review="${index}">记录核查结论</button><button class="button" data-quality-fix="${index}">发起修正</button>`}</div><details><summary>技术详情</summary><p>${e(item.code)} · ${e(item.object_kind)} · ${e(item.object_id)}</p><p>${e(item.detail)}</p><p>${e(item.issue_id)}</p></details></article>`;
  }).join('');
  const sample=(report.review_sample||[]).map((item,index)=>{
    const related=(report.issues||[]).map((issue,i)=>({issue,i})).filter(({issue})=>issue.object_id===item.object_id && issue.object_kind===item.object_kind);
    return `<article class="kb-source"><strong>${e(labels[item.object_id]||'待核查知识记录')}</strong><p>${(item.issue_codes||[]).map(code=>e(ISSUE_LABELS[code]||'知识一致性问题')).join('；')}</p><div class="kb-actions">${historical?'':`<button class="button" data-quality-sample="${index}">查看对象与依据</button>${related.map(({issue,i})=>`<button class="button" data-quality-review="${i}">核查：${e(ISSUE_LABELS[issue.code]||'此项问题')}</button>`).join('')}`}</div><details><summary>抽查定位详情</summary><p>${e(item.object_id)}</p><p>来源片段：${(item.evidence_chunk_ids||[]).map(e).join('、')||'未记录'}</p></details></article>`;
  }).join('');
  return `<article class="kb-source"><h3>${historical?'历史检查报告':'当前图谱质量检查'} · 第 ${report.publication_generation} 版</h3><p><strong>${report.passed?'自动检查通过':'自动检查发现阻断性错误'}${report.total_issue_count?`，有 ${report.total_issue_count} 项发现需关注`:''}</strong></p><p>${counts.canonical_entities??0} 个实体 · ${counts.literal_assertions??0} 条属性 · ${counts.relationship_assertions??0} 条关系 · 阻断性错误 ${report.total_error_count}</p><p class="kb-muted">${historical?'此报告对应历史版本，不代表当前状态。':'检查结果仅对应本次读取的版本。'}自动规则通过不表示事实完整或已完成人工核查。</p><details><summary>版本与审计详情</summary><pre>${e(JSON.stringify({publication_id:report.publication_id,ruleset:report.ruleset_version,run_id:report.run_id,graph_digest:report.graph_digest,ontology_version_id:report.ontology_version_id},null,2))}</pre></details></article>${issues||'<p>本次检查未发现结构或一致性问题。</p>'}<details><summary>建议人工核查 · ${(report.review_sample||[]).length} 个对象</summary><p>从本次发现的问题中选取核查对象。同一份报告的选择保持一致，便于复查；这些对象不代表整个知识库的准确率。</p>${sample||'<p>本次没有需要抽查的问题对象。</p>'}</details>${report.issues_truncated?'<p>问题列表达到显示上限，请使用完整审计报告核查剩余问题。</p>':''}<p class="kb-muted">人工结论独立保存；修正发布后请重新检查。旧结论不会自动应用到新版本。</p>`;
}
export function mountMaintenance({api,epoch,browser,navigate,correct}){
  const renders=new WeakMap();let dialogSerial=0;
  const dialog=document.createElement('dialog');dialog.className='kb-evidence';dialog.setAttribute('aria-label','记录人工核查结论');document.body.append(dialog);dialog.addEventListener('cancel',reset);
  function reset(){dialogSerial++;dialog.close();dialog.replaceChildren();}
  function directoryLabels(report){const d=browser.getDirectory();if(!d || d.pin.publication_id!==report.publication_id || d.pin.corpus_revision!==report.corpus_revision)return {};
    const labels={};for(const n of d.items){labels[n.entity_id]=n.label;for(const f of n.properties)labels[f.revision_id]=labels[f.record_id]=`${n.label} · ${label(f.predicate)}`;for(const f of n.relations)labels[f.revision_id]=labels[f.record_id]=`${n.label} · ${label(f.predicate)} · ${f.other.label}`;}return labels;
  }
  async function openObject(issue,report){
    navigate('browse');browser.tab('entities');await browser.activate();
    const d=browser.getDirectory();
    if(!d || d.pin.publication_id!==report.publication_id || d.pin.corpus_revision!==report.corpus_revision){document.getElementById('kb-dossier').textContent='浏览范围与检查报告版本不一致，请刷新知识并重新检查。';return;}
    const n=d.items.find(n=>n.entity_id===issue.object_id || n.mention_revision_ids.includes(issue.object_id) || [...n.properties,...n.relations].some(f=>f.revision_id===issue.object_id || f.record_id===issue.object_id));
    if(n)browser.select(n.entity_id);else document.getElementById('kb-dossier').textContent='当前授权浏览范围无法定位此对象，请按报告中的技术定位信息核查来源关联或知识模型。';
  }
  async function render(report,container,historical=false){
    const marker={},identity=epoch();renders.set(container,marker);
    const valid=()=>identity===epoch() && renders.get(container)===marker;
    let records=[],labels=historical?{}:directoryLabels(report);
    const paint=()=>{container.innerHTML=qualityMarkup(report,{labels,records,historical});container.querySelectorAll('[data-quality-object]').forEach(b=>b.onclick=()=>openObject(report.issues[Number(b.dataset.qualityObject)],report));container.querySelectorAll('[data-quality-sample]').forEach(b=>b.onclick=()=>openObject(report.review_sample[Number(b.dataset.qualitySample)],report));container.querySelectorAll('[data-quality-fix]').forEach(b=>b.onclick=()=>correct(report.issues[Number(b.dataset.qualityFix)]));container.querySelectorAll('[data-quality-review]').forEach(b=>b.onclick=()=>review(report,report.issues[Number(b.dataset.qualityReview)],()=>render(report,container,historical)));};
    paint();
    if(!historical && !browser.getDirectory()){await browser.activate();if(!valid())return;labels=directoryLabels(report);paint();}
    try{const result=await api('/v1/knowledge/quality/reviews:query',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({run_id:report.run_id})});if(!valid())return;records=result.items;labels={...Object.fromEntries(Object.entries(result.object_labels||{}).map(([key,name])=>[key,name.split(' · ').map((part,i)=>i?label(part):part).join(' · ')])),...labels};paint();if(result.has_more)container.insertAdjacentHTML('beforeend','<p>仅显示最近 100 条人工核查记录；未显示不代表未曾核查。</p>');}
    catch(error){if(valid()){const note=document.createElement('p');note.className='kb-muted';note.textContent=error.status===409?'本次检查尚无可读取的已保存报告；记录核查结论时会先保存并验证版本。':`人工核查记录暂不可用：${error.message}`;container.append(note);}}
  }
  function review(report,issue,refresh){
    const serial=++dialogSerial,identity=epoch();let operation=null,lastBody=null;
    dialog.innerHTML=`<h3>${e(ISSUE_LABELS[issue.code]||'知识问题')}：人工核查</h3><p>仅记录对第 ${report.publication_generation} 版的核查结论，不修改知识或自动检查结果。</p><label>核查结论<select data-decision><option value="NEEDS_CORRECTION">确认需修正</option><option value="NO_CHANGE_REQUIRED">已核查，无需修正</option><option value="PENDING">待核查</option></select></label><label>核查说明<textarea data-notes maxlength="2000" rows="4" placeholder="记录核对的来源及判断依据"></textarea></label><p data-status role="status"></p><div class="kb-actions"><button class="button" data-cancel>取消</button><button class="button primary" data-save>保存核查结论</button></div>`;
    if(!dialog.open)dialog.showModal();dialog.querySelector('[data-cancel]').onclick=reset;
    dialog.querySelector('[data-save]').onclick=async()=>{
      const notes=dialog.querySelector('[data-notes]').value.trim(),decision=dialog.querySelector('[data-decision]').value,status=dialog.querySelector('[data-status]'),button=dialog.querySelector('[data-save]');
      if(!notes){status.textContent='请填写核查说明。';return;}
      const body={run_id:report.run_id,issue_id:issue.issue_id,notes,decision};const signature=JSON.stringify(body);if(lastBody!==signature){operation=crypto.randomUUID();lastBody=signature;}
      button.disabled=true;status.textContent='正在验证版本并保存…';
      try{const saved=await api('/v1/knowledge/quality/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});if(identity!==epoch() || serial!==dialogSerial)return;
        if(saved.report.run_id!==report.run_id)throw new Error('知识版本或检查结果已变化，请重新检查后核查。');
        await api('/v1/knowledge/quality/reviews',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...body,operation_key:operation})});if(identity!==epoch() || serial!==dialogSerial)return;reset();refresh();
      }catch(error){if(identity===epoch() && serial===dialogSerial){status.textContent=error.status===409?'当前版本已变化或请求冲突，请重新检查。':error.status===403?'当前身份需要质量检查与知识复核权限。':error.message;button.disabled=false;}}
    };
  }
  return {render,reset,markup:qualityMarkup,invalidate(container){renders.set(container,{});}};
}
