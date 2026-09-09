/** Business projections only. Canonical IDs control grouping; revisions stay distinct. */
export const LABELS = Object.freeze({Equipment:'设备',Site:'站点',Component:'部件',Organization:'组织',Person:'人员',Inspection:'巡检',Fault:'故障',MaintenanceAction:'维护措施',EquipmentCode:'设备编号',RatedPower:'额定功率',LOCATED_AT:'位于',INSTALLED_AT:'安装于',PART_OF:'组成于',HAS_COMPONENT:'包含部件',INSPECTED_BY:'检查人员',HAS_FAULT:'发生故障'});
export const label = key => LABELS[key] || key;
export const escape = value => String(value ?? '').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export function valueLabel(fact) {
  const s = fact.semantics;
  const raw = s?.raw_value ?? fact.value;
  // Prefer original units; canonical units apply only to unchanged numeric values.
  const unit = s?.raw_unit || (String(raw) === String(s?.canonical_value) ? s?.canonical_unit : null);
  return String(raw) + (unit && !String(raw).trimEnd().endsWith(unit) ? ` ${unit}` : '');
}
export function contextLabel(fact) {
  const s = fact.semantics || {};
  return [['valid_from','起始'],['valid_to','截止'],['observed_at','观测时间']].filter(([k])=>s[k]).map(([k,l])=>`${l}：${s[k]}`).join(' · ');
}
export function dossiers(pages) {
  const nodes = new Map(), facts = new Map();
  let token = null;
  for (const page of pages) {
    if (token && token !== page.view_token) throw new Error('知识版本发生变化，请刷新。');
    token = page.view_token;
    for (const node of page.nodes) nodes.set(node.entity_id, {...node, properties:[], relations:[]});
    for (const fact of [...page.edges,...page.literals]) {
      const old = facts.get(fact.revision_id);
      if (old && JSON.stringify(old)!==JSON.stringify(fact)) throw new Error('事实版本不一致，请刷新。');
      facts.set(fact.revision_id,fact);
    }
  }
  if (nodes.size>500 || facts.size>500) throw new Error('知识范围超过浏览上限，请按来源缩小范围。');
  for (const fact of facts.values()) {
    const subject=nodes.get(fact.source || fact.subject);
    if (!subject || (fact.target && !nodes.has(fact.target))) throw new Error('知识关系端点不完整。');
    if (fact.target) {
      subject.relations.push({...fact,direction:'outgoing',other:nodes.get(fact.target)});
      if(fact.target!==fact.source) nodes.get(fact.target).relations.push({...fact,direction:'incoming',other:subject});
    } else subject.properties.push(fact);
  }
  return [...nodes.values()].sort((a,b)=>a.label.localeCompare(b.label,'zh-CN') || a.entity_id.localeCompare(b.entity_id));
}
export function entityPage(items, {query='',type='',page=0,size=12}={}) {
  const q=query.trim().toLocaleLowerCase();
  const found=items.filter(n=>(!type || n.entity_type===type) && (!q || [n.label,...(n.aliases||[]),...n.properties.map(f=>f.value)].join(' ').toLocaleLowerCase().includes(q)));
  const current=Math.min(Math.max(0,page),Math.max(0,Math.ceil(found.length/size)-1));
  return {items:found.slice(current*size,(current+1)*size),total:found.length,page:current,pages:Math.ceil(found.length/size)};
}
/** Collect all bounded pages before entity grouping. No partial dossier is published. */
export async function readDirectory(api, version_filter={}, isCurrent=()=>true) {
  const pages=[], cursors=new Set(); let view_token, cursor;
  for(let count=0;count<12;count++) {
    const page=await api('/v1/knowledge/graph:query',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({page_size:200,version_filter,...(view_token?{view_token}:{}),...(cursor?{cursor}:{})})});
    if(!isCurrent()) throw new Error('读取已取消。');
    if(view_token && page.view_token!==view_token) throw new Error('知识版本发生变化，请刷新。');
    pages.push(page); view_token=page.view_token;
    if(!page.page.has_more) return {pages,items:dossiers(pages),view_token,pin:page.pin,schema:page.schema};
    cursor=page.page.next_cursor;
    if(!cursor || cursors.has(cursor)) throw new Error('知识分页无效，请刷新。');
    cursors.add(cursor);
  }
  throw new Error('知识范围超过浏览上限，请按来源缩小范围。');
}
