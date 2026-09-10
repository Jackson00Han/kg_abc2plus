import {$, element, button, clear, toast, safeError} from './core.mjs';

/** Local project/role picker; the server owns tenant IDs and all permissions. */
export function mountWorkspacePicker({bootstrap, client, onChange}) {
  if (!bootstrap.knowledge_bases?.length) return null;
  const picker=$('knowledge-base'), roles=$('persona');
  const create=$('create-knowledge-base'), reset=$('reset-knowledge-base');
  let busy=false, modal=null, serial=0;
  $('knowledge-base-picker').hidden=false;
  function selectedProject() {return bootstrap.knowledge_bases.find(p=>p.knowledge_base_id===picker.value);}
  function selectedRole() {return bootstrap.personas.find(p=>p.id===roles.value)?.role||'admin';}
  function updateControls() {
    const admin=selectedRole()==='admin';
    create.disabled=busy||!admin;reset.disabled=busy||!admin;
    reset.hidden=!admin;
    $('workspace-reset-note').textContent=selectedProject()
      ? `当前知识库：${selectedProject().name}。重置仅影响这个项目。` : '';
  }
  function render(id, role='admin') {
    clear(picker);
    for(const project of bootstrap.knowledge_bases) picker.append(new Option(project.name,project.knowledge_base_id));
    picker.value=bootstrap.knowledge_bases.some(p=>p.knowledge_base_id===id)?id:bootstrap.default_knowledge_base_id;
    clear(roles);
    const people=bootstrap.personas.filter(p=>p.knowledge_base_id===picker.value);
    for(const person of people) roles.append(new Option(person.role==='admin'?'管理员':'用户',person.id));
    roles.value=(people.find(p=>p.role===role)||people[0]).id;
    roles.disabled=false;
    updateControls();
  }
  async function enter() {
    serial++;modal?.close();modal=null;
    updateControls();
    try {sessionStorage.setItem('industrial-workspace-selection',JSON.stringify({id:picker.value,role:selectedRole()}));} catch {}
    await onChange(roles.value);
  }
  async function apply(payload, projectId, role='admin') {
    Object.assign(bootstrap,payload);
    render(projectId,role);
    await enter();
  }
  function dialog(title) {
    modal?.close();
    const node=element('dialog','source-dialog workspace-dialog');
    node.setAttribute('aria-label',title);
    const head=element('div','dialog-heading workspace-dialog-heading');
    head.append(element('h2','',title),button('关闭',()=>node.close(),'button workspace-dialog-close'));
    node.append(head);
    node.addEventListener('close',()=>{node.remove();if(modal===node)modal=null;});
    document.body.append(node);node.showModal();modal=node;
    return node;
  }
  function setBusy(value) {busy=value;picker.disabled=value;roles.disabled=value;updateControls();}
  function createProject() {
    const node=dialog('新建独立知识库');
    node.append(element('p','muted','用于一个独立项目。本体、原始资料、候选事实与发布历史分别保存，不会与其他知识库串联。'));
    const form=element('form','workspace-form'),label=element('label','','知识库名称');
    const input=element('input');input.required=true;input.maxLength=80;input.placeholder='例如：北辰泵站改造项目';
    label.append(input);
    const status=element('p','inline-status');status.setAttribute('role','status');
    const submit=element('button','button primary','创建并进入');submit.type='submit';
    form.append(label,status,submit);node.append(form);input.focus();
    const operation=crypto.randomUUID(),identity=client.epoch;
    form.addEventListener('submit',async event=>{
      event.preventDefault();if(busy)return;
      const name=input.value.trim();if(!name){status.textContent='请输入知识库名称。';return;}
      setBusy(true);submit.disabled=true;status.textContent='正在建立独立知识库…';
      try {
        const result=await client.request('/playground/workspaces',{name,operation_id:operation});
        if(identity!==client.epoch)return;
        node.close();await apply(result,result.knowledge_base.knowledge_base_id);
        toast('知识库已建立，请从专家本体开始。');
      } catch(error) {if(identity===client.epoch)status.textContent=safeError(error)||'创建未完成，请刷新列表核对后重试。';}
      finally {setBusy(false);submit.disabled=false;}
    });
  }
  async function resetProject() {
    const project=selectedProject();if(!project||busy)return;
    const request=++serial,identity=client.epoch;
    const node=dialog(`重置“${project.name}”`);
    const status=element('p','inline-status','正在核对清理范围…');status.setAttribute('role','status');node.append(status);
    try {
      const preview=await client.request(`/playground/workspaces/${encodeURIComponent(project.knowledge_base_id)}/reset-preview`,undefined,{method:'GET'});
      if(request!==serial||identity!==client.epoch||!node.open)return;
      status.textContent='';
      const counts=element('div','workspace-reset-counts');
      for(const [key,label] of [['documents','份原始资料'],['ontologies','个本体版本'],['records','条知识记录'],['publications','个发布版本']]) {
        const stat=element('div');stat.append(element('strong','',preview.counts[key]),element('span','',label));counts.append(stat);
      }
      node.append(counts,element('p','','本操作会清空当前可用的本体、资料、候选与发布内容，让这个知识库重新开始。其他项目保持原状。'),
        element('p','source-note','原有证据与审核历史会隔离归档，不再参与当前库的浏览、检索或发布。当前库的所有旧会话同时失效。'));
      if(preview.running_tasks){status.textContent='当前库仍有写入任务运行，请等待上传、审核或发布结束后重新打开此面板。';return;}
      const form=element('form','workspace-form'),label=element('label','',`输入知识库名称“${preview.name}”确认`),input=element('input');
      input.required=true;input.autocomplete='off';input.maxLength=80;label.append(input);
      const submit=element('button','button danger-button','确认重置当前知识库');submit.type='submit';submit.disabled=true;
      input.addEventListener('input',()=>submit.disabled=input.value.trim()!==preview.name);
      form.append(label,submit);node.append(form);input.focus();
      const operation=crypto.randomUUID();
      form.addEventListener('submit',async event=>{
        event.preventDefault();if(busy||input.value.trim()!==preview.name)return;
        setBusy(true);submit.disabled=true;status.textContent='正在建立新的空白知识库代次…';
        try {
          const result=await client.request(`/playground/workspaces/${encodeURIComponent(project.knowledge_base_id)}/reset`,{
            name:preview.name,expected_generation:preview.generation,operation_id:operation,
            confirmation:'RESET_CURRENT_KNOWLEDGE_BASE',
          });
          if(identity!==client.epoch)return;
          node.close();await apply(result,project.knowledge_base_id);
          toast('当前知识库已重置，可以重新搭建。');
        } catch(error) {if(identity===client.epoch)status.textContent=safeError(error)||'重置状态需重新核对，请刷新当前页面。';}
        finally {setBusy(false);submit.disabled=false;}
      });
    } catch(error) {if(identity===client.epoch&&request===serial)status.textContent=safeError(error);}
  }
  picker.addEventListener('change',()=>{const role=selectedRole();render(picker.value,role);void enter();});
  roles.addEventListener('change',()=>{
    modal?.close();updateControls();
    try {sessionStorage.setItem('industrial-workspace-selection',JSON.stringify({id:picker.value,role:selectedRole()}));} catch {}
  });
  create.addEventListener('click',createProject);reset.addEventListener('click',()=>void resetProject());
  let initial={id:bootstrap.default_knowledge_base_id,role:'admin'};
  try {initial={...initial,...JSON.parse(sessionStorage.getItem('industrial-workspace-selection')||'{}')};}catch{}
  render(initial.id,initial.role);
  return {updateControls,async refresh(){
    if(busy)return;
    const id=picker.value,role=selectedRole(),request=++serial;
    const response=await fetch('/playground/bootstrap',{cache:'no-store'});
    if(!response.ok)throw new Error('无法刷新知识库列表。');
    const result=await response.json();if(request!==serial)return;
    await apply(result,id,role);
  }};
}
