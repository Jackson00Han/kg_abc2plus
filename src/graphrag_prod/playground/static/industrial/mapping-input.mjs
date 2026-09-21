/** Optional, source-scoped declarative mappings. No domain/file-name inference. */
export const MAX_MAPPING_BYTES = 262144;
const SUPPORTED = Object.freeze({
  'xml-declarative-mapping:v1': {mime:'application/xml', label:'XML 映射', chunking:{max_chars:65536,strategy:'mapped_document'}},
  'markdown-section-mapping:v1': {mime:'text/markdown', label:'Markdown 映射', chunking:{max_chars:3000,strategy:'structure'}},
});
const escape = value => String(value ?? '').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

export function parseMappingJson(text) {
  if (typeof text !== 'string' || new TextEncoder().encode(text).length > MAX_MAPPING_BYTES)
    throw new Error('映射配置不能超过 256 KiB。');
  // Parse syntax first, then walk the original tokens to reject duplicate keys;
  // JSON.parse alone would silently discard an earlier mapping declaration.
  let parsed;
  try {parsed=JSON.parse(text);} catch {throw new Error('映射配置不是有效 JSON。');}
  let position=0, count=0;
  const space=()=>{while (/\s/.test(text[position] || '') && position<text.length) position++;};
  const string=()=>{const start=position++;while(position<text.length){const char=text[position++];if(char==='\\')position++;else if(char==='"')return JSON.parse(text.slice(start,position));}throw new Error('JSON 字符串未闭合。');};
  function walk(depth) {
    if(depth>32 || ++count>100000)throw new Error('映射配置的层级或字段数量超过上限。');
    space();const char=text[position];
    if(char==='{' || char==='['){position++;space();const end=char==='{'?'}':']',keys=new Set();
      while(text[position]!==end){
        if(char==='{'){const key=string();if(keys.has(key))throw new Error('映射配置存在重复字段，不能静默覆盖。');keys.add(key);space();position++;}
        walk(depth+1);space();if(text[position]===','){position++;space();}else break;
      }
      position++;return;
    }
    if(char==='"'){string();return;}
    const start=position;while(position<text.length && !/[\s,\]}]/.test(text[position]))position++;
    const value=JSON.parse(text.slice(start,position));if(typeof value==='number'&&!Number.isFinite(value))throw new Error('映射配置不能包含非有限数值。');
  }
  walk(0);
  if(!parsed || Array.isArray(parsed) || typeof parsed!=='object' || typeof parsed.version!=='string' || !Object.hasOwn(SUPPORTED,parsed.version))
    throw new Error('请选择受支持的版本化 XML 或 Markdown 声明式映射配置。');
  for(const key of ['mapping_id','mapping_version'])if(typeof parsed[key]!=='string'||!parsed[key].trim()||parsed[key].length>512)
    throw new Error('映射配置需要有界的 mapping_id 和 mapping_version。');
  return parsed;
}

export function mappingPreflightMarkup(report) {
  if(!report)return '';
  const unresolved=(report.relationships||[]).filter(r=>r.status==='UNRESOLVED').length;
  const gaps=(report.unresolved_references||[]).length;
  const findings=[...(report.diagnostics||[]),...(report.ontology_readiness?.findings||[])];
  const diagnostics=findings.slice(0,30), valid=report.valid&&!findings.some(d=>d.severity==='error');
  const simulation=report.semantic_context?.content_layer==='simulated_knowledge';
  return `<section class="chunking-preview" data-mapping-preflight><strong>${valid?'映射预检完成':'映射需要修正'}</strong><p>识别 ${(report.entities||[]).length} 个对象 · ${(report.relationships||[]).length} 条关系声明${unresolved?` · ${unresolved} 条关系待补定义`:''}${gaps?` · ${gaps} 项未解析引用`:''}。</p><p class="field-note">${simulation?'模拟知识只表达概念与候选解释，不代表现场事件或工业验证结论。':'原文与映射仍需核对；来源等级、候选审核与发布分别保留。'}</p>${diagnostics.length?`<details><summary>查看诊断与缺项（${findings.length>30?`前 30 / ${findings.length}`:findings.length}）</summary>${diagnostics.map(d=>`<p>${escape(d.message||d.detail||d.code)}</p>`).join('')}</details>`:''}</section>`;
}

export function mountMappingInput({host, epoch, busy, onChange}) {
  const holder=document.createElement('div');holder.className='field full';
  holder.innerHTML='<label for="document-mapping-file">声明式映射（可选）</label><p class="field-note">已有经过审校的字段或章节映射时，可由程序按原文构建，减少重复模型提取。映射不会自动批准或发布知识。</p><div class="workbench-actions"><button class="button" type="button" id="document-mapping-choose">选择映射 JSON</button><button class="button" type="button" id="document-mapping-clear" hidden>清除映射</button><input id="document-mapping-file" type="file" accept=".json,application/json" hidden></div><p class="field-note" id="document-mapping-status" role="status">未加载映射，沿用所选资料处理方式。</p>';
  host.querySelector('#document-extraction-mode').closest('.field').before(holder);
  const file=holder.querySelector('input'),choose=holder.querySelector('#document-mapping-choose'),clearButton=holder.querySelector('#document-mapping-clear'),status=holder.querySelector('#document-mapping-status');
  let mapping=null, loading=false, error='', serial=0, filename='';
  function render(){choose.disabled=busy()||loading;clearButton.disabled=busy()||loading;file.disabled=busy()||loading;clearButton.hidden=!mapping&&!error;
    status.textContent=loading?'正在读取映射配置…':error|| (mapping?`${SUPPORTED[mapping.version].label} · ${mapping.mapping_id} · v${mapping.mapping_version} · ${filename}。上传前将核对本体、原文结构及缺项。`:'未加载映射，沿用所选资料处理方式。');
    status.classList.toggle('danger-text',Boolean(error));}
  function clear({notify=true}={}){serial++;mapping=null;loading=false;error='';filename='';file.value='';render();if(notify)onChange();}
  choose.addEventListener('click',()=>{if(!busy())file.click();});
  clearButton.addEventListener('click',()=>{if(!busy())clear();});
  file.addEventListener('change',async()=>{if(busy())return;const selected=file.files?.[0];if(!selected)return;
    const request=++serial, identity=epoch();mapping=null;error='';loading=true;filename=selected.name;render();onChange();
    try {
      if(!selected.size||selected.size>MAX_MAPPING_BYTES)throw new Error('映射配置必须在 1 byte 到 256 KiB 之间。');
      const text=new TextDecoder('utf-8',{fatal:true}).decode(await selected.arrayBuffer());
      const value=parseMappingJson(text);if(request!==serial||identity!==epoch())return;mapping=value;
    } catch(cause){if(request!==serial||identity!==epoch())return;error=cause instanceof TypeError?'映射配置必须为 UTF-8 文本。':cause.message;}
    finally {if(request===serial&&identity===epoch()){loading=false;render();onChange();}}
  });
  render();
  return {
    clear, sync:render,
    get active(){return Boolean(mapping);},
    get pending(){return loading||Boolean(error);},
    chunking(){return mapping?{...SUPPORTED[mapping.version].chunking}:null;},
    request(mime){if(loading)throw new Error('请等待映射配置读取完成。');if(error)throw new Error(error);if(!mapping)return null;
      if(SUPPORTED[mapping.version].mime!==mime)throw new Error('映射格式与所选文档不匹配，请重新选择对应的映射配置。');
      return JSON.parse(JSON.stringify(mapping));},
    checkPreflight(report){if(!mapping)return;if(!report||!report.valid)throw new Error('映射预检未通过，请先查看预览中的诊断并修正映射。');
      if([...(report.diagnostics||[]),...(report.ontology_readiness?.findings||[])].some(item=>item.severity==='error'))throw new Error('映射存在本体或字段约束问题，请修正后再构建。');},
  };
}
