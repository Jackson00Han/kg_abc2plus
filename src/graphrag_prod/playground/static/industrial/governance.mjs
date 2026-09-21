import template from './governance-template.mjs';
import {instanceExportFile} from './instance-export.mjs';
import {toast, beginButtonFeedback, clearButtonFeedback} from './core.mjs';
import {mountMappingInput, mappingPreflightMarkup} from './mapping-input.mjs';

/** Mature review/publication workflow mounted in the Industrial shell. */
export async function mountGovernance(host, {client, bootstrap, onNavigate = () => {}, onPublished = async () => {}, onExplore=null, onSource=null}) {
  host.classList.add('industrial-governance');
  host.innerHTML=template;
  const $=id=>host.querySelector(`[id="${id}"]`);
  let lastBuildFlow='business', buildStep='upload', loadedIdentity=null, loadingIdentity=null;

      const state = {
        bootstrap: null,

        ontologies: [],
        ontologyRuleRegistry: null,
        ontologyRuleError: '',
        ontologyInputError: '',
        ontologyFileSelection: 0,
        ontologyFileLoading: false,
        ontologyRuleLoading: false,
        ontologyValidating: false,
        ontologyValidation: null,
        ontologyFilename: '',
        ontologyExpectedChecksum: null,
        reviews: [],
        reviewEpoch: 0,
        resolutions: new Map(),
        resolutionQueue: [],
        resolutionActive: 0,
        revisionHistories: new Map(),
        constructionJobs: [],
        constructionOperation: null,
        constructionBusy: false,
        latestAutoReview: null,
        autoReviewBusy: false,
        autoReviewEpoch: 0,
        constructionDetailRequests: new Map(),
        constructionReceipt: null,
        demoSourceBinding: null,
        constructionFlow: 'business',
        uploadKnowledgeScope: 'BUSINESS',
        buildView: null,
        lastConstructionMode: null,
        expertRevisionIds: [],
        expertImportMayReplace: false,
        expertImportBusy: false,
        expertPublishing: false,
        publications: [],
        publicationCandidates: [],
        publicationCandidateOffset: 0,
        publicationCandidateTotal: 0,
        publicationCandidateEntityTotal: 0,
        publicationCandidatesLoading: false,
        publicationBulkBusy: false,
        publicationCandidatesRequestEpoch: 0,
        selectedPublicationCandidates: new Map(),
        selectedCandidateRevisions: new Set(),
        approvedRevisions: new Set(),
        activeInventory: null,
        inventoryEpoch: 0,
        selectedInventoryRevisions: new Set(),
        inventoryRevisionHistories: new Map(),
        inventoryHistoryRequests: new Map(),
        inventoryRemovalRecordIds: new Set(),
        quality: null,
        qualityEpoch: 0,
        qualityHistory: [],
        qualityHistoryEpoch: 0,
        qualityDetailEpoch: 0,
        qualitySaveEpoch: 0,
        qualitySaving: false,
        activeDocuments: [],
      };


const elements = {
        ontologyEditor: $('ontology-editor'), ontologyList: $('ontology-list'),
        aboxEditor: $('abox-editor'), aboxOutput: $('abox-output'),
        constructionOutput: $('construction-output'), constructionJobList: $('construction-job-list'), reviewList: $('review-list'),
        publicationRevisions: $('publication-revisions'), publicationRemovals: $('publication-removals'), publicationOutput: $('publication-output'),
        publicationCandidateList: $('publication-candidate-list'), historyList: $('history-list'),
        inventoryDocumentFilter: $('inventory-document-filter'), inventoryLimit: $('inventory-limit'),
        inventorySummary: $('inventory-summary'), inventoryList: $('inventory-list'),
        qualityContent: $('quality-content'),
        qualitySaveButton: $('quality-save-button'), qualitySaveOutput: $('quality-save-output'),
        qualityHistoryPublication: $('quality-history-publication'), qualityHistoryList: $('quality-history-list'),
        qualityHistoryDetail: $('quality-history-detail'),
        documentLifecycleSummary: $('document-lifecycle-summary'),
        documentLifecycleList: $('document-lifecycle-list'),
      };


      const escapeHtml = value => String(value ?? '').replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
      const shortId = value => value ? `${String(value).slice(0, 8)}…${String(value).slice(-5)}` : '—';
      const number = value => new Intl.NumberFormat('zh-CN').format(value ?? 0);
      const MAX_UPLOAD_BYTES = 5 * 1024 * 1024;
      const MAX_ONTOLOGY_BYTES = 1_000_000;
      const mimeByExtension = {txt:'text/plain', md:'text/markdown', markdown:'text/markdown', csv:'text/csv', json:'application/json', xml:'application/xml'};



  state.bootstrap=bootstrap;
  Object.defineProperty(state,'identityEpoch',{get:()=>client.epoch});
  const currentPersona=()=>bootstrap.personas.find(item=>item.id===client.personaId)||null;
  const showToast=toast;
  // The full user action owns its button, including follow-up reads.
  function beginOperationFeedback(_label, {button=null}={}) {
    return beginButtonFeedback(button, {disable:false});
  }
  function foregroundAction(_label, action) {
    return async event => {
      const button=event?.currentTarget;
      if(button?.getAttribute('aria-busy')==='true') return;
      const disabled=button?.disabled;
      if(button) button.disabled=true;
      const finish=beginOperationFeedback('',{button});
      try {return await action(event);} finally {
        finish();
        if(button?.isConnected) button.disabled=Boolean(disabled);
        if(button?.id==='ontology-import-button' || button?.id==='ontology-validate-button') syncOntologyControls();
        setReviewBusy(Boolean(state.reviewBusy));
      }
    };
  }
  const apiRequest=async(url,options={})=>{
    const identity=client.epoch;
    const {feedback,...requestOptions}=options;
    const result=await client.requestOptions(url,requestOptions);
    if(identity===client.epoch && options.method==='POST' && (/publications:publish$/.test(url)||/:rollback$/.test(url))) {
      void Promise.resolve(onPublished([])).catch(error=>showToast(error.message));
    }
    return result;
  };
  const uploadMapping=mountMappingInput({host,epoch:()=>state.identityEpoch,busy:()=>state.constructionBusy,
    onChange:()=>{clearConstructionResult();completeConstructionOperation();updateChunkingSummary();updateConstructionMode();}});
  function uploadContext() {
    const family=$('document-family').value;
    if(currentPersona()?.tenant_id!=='industrial-schneider-demo'||!family) return null;
    if($('document-tbox').value.trim()!=='industrial-electric-v1') throw new Error('产品族分类适用于 industrial-electric-v1 本体。自定义本体请选择通用资料。');
    const asset=$('document-asset').value.trim();
    return {family,asset_keys:asset?[asset]:[]};
  }
  function defaultDocumentUri(name) {
    return $('document-family').value && currentPersona()?.tenant_id==='industrial-schneider-demo'
      ? `industrial-upload://industrial-schneider-demo/${name}` : `urn:local:controlled-upload:${name}`;
  }

      function renderDocumentAccessGroups() {
        const container = $('document-access-groups');
        const note = $('document-access-note');
        const persona = currentPersona();
        const available = Array.isArray(persona?.groups) && persona.groups.includes('members');
        container.innerHTML = `<select id="document-access-group" name="document-access-group" required ${available ? '' : 'disabled'}><option value="members">假想的用户组-1</option></select>`;
        note.textContent = available
          ? '当前知识库管理员默认可见；用户分组功能后续完善。'
          : '当前身份尚未配置可分配的用户组。';
      }

      function selectedDocumentAccessGroups() {
        const selected = $('document-access-group');
        const groups = currentPersona()?.groups || [];
        if (!selected || selected.disabled || selected.value !== 'members' || !groups.includes('members')) return [];
        // User-group selection never removes the current workspace administrators.
        return groups.includes('administrators') ? ['members', 'administrators'] : ['members'];
      }


      function parseJsonEditor(element, label) {
        try {
          const value = JSON.parse(element.value);
          if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error();
          return value;
        } catch (_) {
          throw new Error(`${label}必须是一个有效 JSON object`);
        }
      }

      function output(element, value) {
        element.hidden = false;
        element.classList.remove('upload-check');
        element.textContent = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
        if (element === elements.constructionOutput || element === elements.publicationOutput) element.scrollIntoView?.({block:'nearest',behavior:'smooth'});
      }


      function provenanceBadges(provenance) {
        if (!provenance || (!provenance.authority && !provenance.origin)) return '';
        const authorityClass = provenance.authority === 'AUTHORITATIVE' ? ' authoritative' : '';
        return `<div class="badge-row">
          <span class="trust-badge${authorityClass}">${escapeHtml(({AUTHORITATIVE:'权威', SECONDARY:'次权威'})[provenance.authority] || provenance.authority)}</span>
          <span class="trust-badge">${escapeHtml(({AUTHORITATIVE_EXTRACTED:'权威文档抽取', LLM_EXTRACTED:'业务文档抽取', MAPPED:'声明式映射', AUTHORITATIVE_MAPPED:'权威资料映射', HUMAN_SUPPLEMENT:'人工补充', EXPERT_IMPORT:'历史专家导入', EXPERT_CREATED:'历史专家创建'})[provenance.origin] || provenance.origin)}</span>
          ${provenance.status ? `<span class="trust-badge">${escapeHtml(provenance.status)}</span>` : ''}
          ${provenance.confidence != null ? `<span class="trust-badge">confidence ${escapeHtml(provenance.confidence)}</span>` : ''}
        </div>`;
      }

      function graphEvidenceMarkup(evidence) {
        if (!evidence) return '';
        const citation = evidence.citation || {};
        return `${provenanceBadges(evidence.provenance)}<div class="exact-evidence"><strong>${citation.canonical_uri?.startsWith('urn:graphrag:human:') ? '人工补充记录' : '文档原文证据'}</strong> · ${escapeHtml(citation.document_title || citation.source_name)} · Chunk <span title="${escapeHtml(citation.chunk_id)}">${escapeHtml(shortId(citation.chunk_id))}</span> · chars ${escapeHtml(evidence.char_start)}:${escapeHtml(evidence.char_end)}<br>${escapeHtml(evidence.quoted_text)}</div>`;
      }

      function literalSemantics(item) {
        if (item?.literal_semantics && typeof item.literal_semantics === 'object') {
          return item.literal_semantics;
        }
        // Keep old local snapshots readable while treating the versioned nested
        // response as authoritative. Unknown/new nested fields are retained.
        const fieldAliases = {
          datatype: ['datatype', 'literal_datatype'],
          typed_value: ['typed_value', 'literal_typed_value'],
          raw_value: ['raw_value', 'literal_raw_value'],
          raw_unit: ['raw_unit', 'literal_raw_unit'],
          canonical_value: ['canonical_value', 'literal_canonical_value'],
          canonical_unit: ['canonical_unit', 'literal_canonical_unit'],
          valid_from: ['valid_from', 'literal_valid_from'],
          valid_to: ['valid_to', 'literal_valid_to'],
          observed_at: ['observed_at', 'literal_observed_at'],
          raw_valid_from: ['raw_valid_from', 'literal_raw_valid_from'],
          raw_valid_to: ['raw_valid_to', 'literal_raw_valid_to'],
          raw_observed_at: ['raw_observed_at', 'literal_raw_observed_at'],
        };
        return Object.entries(fieldAliases).reduce((result, [field, aliases]) => {
          const found = aliases.find(alias => item?.[alias] != null);
          if (found) result[field] = item[found];
          return result;
        }, {});
      }

      function literalSemanticsMarkup(item) {
        const semantics = literalSemantics(item);
        return Object.keys(semantics).length
          ? `<p>${escapeHtml(JSON.stringify(semantics, null, 2))}</p>`
          : '';
      }

      function relationshipPropertiesMarkup(item, graphEvidence = false) {
        const values = Array.isArray(item?.relationship_properties) ? item.relationship_properties : [];
        if (!values.length) return '';
        return `<div class="exact-evidence"><strong>关系属性（强类型 + 独立证据）</strong>${values.map(value => {
          const semantics = literalSemantics(value);
          const normalized = `${semantics.canonical_value ?? semantics.raw_value ?? '—'}${semantics.canonical_unit ? ` ${semantics.canonical_unit}` : ''}`;
          const evidence = value.evidence || {};
          const evidenceMarkup = graphEvidence
            ? graphEvidenceMarkup(evidence)
            : `<div>Chunk ${escapeHtml(shortId(evidence.chunk_id))} · chars ${escapeHtml(evidence.char_start)}:${escapeHtml(evidence.char_end)} · ${escapeHtml(evidence.quoted_text)}</div>`;
          return `<div style="margin-top:8px"><strong>${escapeHtml(value.name)} = ${escapeHtml(normalized)}</strong> · confidence ${escapeHtml(value.confidence)}${literalSemanticsMarkup(value)}${evidenceMarkup}</div>`;
        }).join('')}</div>`;
      }


      function activeOntology(key) {
        return state.ontologies.find(item => item.key === key && item.status === 'PUBLISHED') || null;
      }

      function renderOntologies() {
        const available=$('available-ontologies');
        if(available) available.innerHTML=state.ontologies.filter(item=>item.status==='PUBLISHED').map(item=>`<option value="${escapeHtml(item.key)}">${escapeHtml(item.key)} · v${escapeHtml(item.import_capabilities?.source_version || item.source_contract?.metadata?.ontology_version || item.version)}</option>`).join('');
        renderConstructionOntology();
        if (!state.ontologies.length) {
          elements.ontologyList.innerHTML = '<div class="output-box">尚未配置本体。上传 YAML 文件并通过后台校验后，保存本体版本。</div>';
          return;
        }
        elements.ontologyList.innerHTML = state.ontologies.map((item, index) => {
          const entityNames = (item.entity_types || []).map(value => value.name).join(', ');
          const relationNames = (item.relationship_types || []).map(value => value.name).join(', ');
          const relationshipContracts = (item.relationship_types || []).map(value => {
            const properties = (value.properties || []).map(property => `${escapeHtml(property.name)}:${escapeHtml(property.datatype)} ${escapeHtml(property.cardinality)}${property.unit ? ` [${escapeHtml(property.unit)}]` : ''}`).join(', ');
            return `${escapeHtml(value.name)} · source ${escapeHtml(value.source_cardinality || 'ZERO_OR_MORE')} · target ${escapeHtml(value.target_cardinality || 'ZERO_OR_MORE')}${properties ? ` · properties ${properties}` : ''}`;
          }).join('<br>');
          const capabilities = item.import_capabilities;
          const capabilityMarkup = capabilities ? `<details class="ontology-capabilities"><summary>当前启用范围与约束</summary><p>${escapeHtml(capabilities.scope)}</p><p>不允许创建实例的类型：${escapeHtml((capabilities.blocked_entity_types || []).join('、') || '无')}<br>不允许直接抽取为实例事实的关系：${escapeHtml((capabilities.blocked_relationship_types || []).join('、') || '无')}</p><p>完整来源定义与版本已保留，可通过“导出 JSON”复核。</p></details>` : '';
          const definitions = `<p>Entities: ${escapeHtml(entityNames || '—')}<br>Relations: ${escapeHtml(relationNames || '—')}<br>${relationshipContracts || 'Relationship contracts: —'}</p>`;
          const definitionMarkup = capabilities
            ? `<p>完整保留 ${item.entity_types.length} 类实体、${item.relationship_types.length} 类关系定义；可用于普通抽取的实体类型 ${item.entity_types.length - capabilities.blocked_entity_types.length} 类、关系 ${item.relationship_types.length - capabilities.blocked_relationship_types.length} 类。</p>${capabilityMarkup}<details class="ontology-definitions"><summary>查看类型与关系定义</summary>${definitions}</details>`
            : definitions;
          return `<article class="governance-item"><div class="governance-item-head"><div><strong>${escapeHtml(item.key)} · v${escapeHtml(item.import_capabilities?.source_version || item.source_contract?.metadata?.ontology_version || item.version)}</strong><p>${escapeHtml(item.tbox_id)}</p>${definitionMarkup}</div><span class="trust-badge ${item.status === 'PUBLISHED' ? 'authoritative' : ''}">${escapeHtml({DRAFT:'未启用',PUBLISHED:'已启用',RETIRED:'已停用'}[item.status] || item.status)}</span></div><div class="workbench-actions"><button class="button" type="button" data-load-tbox="${index}">载入并校验</button><button class="button" type="button" data-copy-tbox="${index}">复制为下一版本</button><button class="button" type="button" data-download-tbox="${index}">导出 JSON</button>${item.status === 'DRAFT' ? `<button class="button primary" type="button" data-publish-tbox="${index}">启用此版本</button>` : ''}</div></article>`;
        }).join('');
        elements.ontologyList.querySelectorAll('[data-load-tbox]').forEach(button => {
          button.addEventListener('click', foregroundAction('', () => loadOntologyIntoEditor(Number(button.dataset.loadTbox), false)));
        });
        elements.ontologyList.querySelectorAll('[data-copy-tbox]').forEach(button => {
          button.addEventListener('click', foregroundAction('', () => loadOntologyIntoEditor(Number(button.dataset.copyTbox), true)));
        });
        elements.ontologyList.querySelectorAll('[data-download-tbox]').forEach(button => {
          button.addEventListener('click', foregroundAction('', () => downloadOntology(Number(button.dataset.downloadTbox))));
        });
        elements.ontologyList.querySelectorAll('[data-publish-tbox]').forEach(button => {
          button.addEventListener('click', foregroundAction('', () => publishOntology(Number(button.dataset.publishTbox))));
        });
      }

      function editableOntology(item, nextVersion = false) {
        const versions = state.ontologies
          .filter(candidate => candidate.key === item.key)
          .map(candidate => Number(candidate.version))
          .filter(Number.isSafeInteger);
        const sourceContract = item.source_contract_json ? JSON.parse(item.source_contract_json) : item.source_contract;
        if (sourceContract?.metadata) {
          const source = JSON.parse(JSON.stringify(sourceContract));
          if (nextVersion) {
            const next = Math.max(0, ...versions) + 1;
            source.metadata.ontology_version = `${Math.floor(next / 1000000)}.${Math.floor(next / 1000) % 1000}.${next % 1000}`;
            source.metadata.status = 'DRAFT';
          } else source.expected_checksum = item.checksum;
          if (item.rule_reference_registry) source.rule_reference_registry = JSON.parse(JSON.stringify(item.rule_reference_registry));
          return source;
        }
        const definition = {
          key: item.key,
          version: nextVersion ? Math.max(0, ...versions) + 1 : item.version,
          description: item.description ?? null,
          entity_types: JSON.parse(JSON.stringify(item.entity_types || [])),
          relationship_types: JSON.parse(JSON.stringify(item.relationship_types || [])),
        };
        if (item.hierarchies?.length) definition.hierarchies = JSON.parse(JSON.stringify(item.hierarchies));
        if (definition.description == null) delete definition.description;
        if (!nextVersion) definition.expected_checksum = item.checksum;
        return definition;
      }

      async function loadOntologyIntoEditor(index, nextVersion) {
        const item = state.ontologies[index];
        if (!item || state.ontologySaving) return;
        clearOntologyInput();
        elements.ontologyEditor.value = JSON.stringify(editableOntology(item, nextVersion), null, 2);
        state.ontologyFilename = `${item.key}.json`;
        state.ontologyExpectedChecksum = nextVersion ? null : item.checksum;
        $('ontology-file-name').textContent = `${item.key} · ${nextVersion ? '下一版本草稿' : '已存版本'}`;
        $('ontology-source-details').open = true;
        elements.ontologyEditor.focus();
        elements.ontologyEditor.setSelectionRange?.(0, 0);
        elements.ontologyEditor.scrollTop = 0;
        await validateOntologyEditor();
      }

      function downloadOntology(index) {
        const item = state.ontologies[index];
        if (!item) return;
        const artifact = {
          schema: 'graphrag-property-tbox-export-v1',
          exported_tbox_id: item.tbox_id,
          status: item.status,
          checksum: item.checksum,
          definition: editableOntology(item, false),
        };
        const blob = new Blob([`${JSON.stringify(artifact, null, 2)}\n`], {type: 'application/json'});
        const link = document.createElement('a');
        const safeKey = String(item.key).replace(/[^A-Za-z0-9._-]+/g, '-');
        link.href = URL.createObjectURL(blob);
        link.download = `${safeKey}-v${item.import_capabilities?.source_version || item.version}-tbox.json`;
        document.body.appendChild(link);
        link.click();
        const objectUrl = link.href;
        link.remove();
        URL.revokeObjectURL(objectUrl);
        showToast(`已导出 ${item.key} v${item.import_capabilities?.source_version || item.version}`);
      }

      async function loadOntologies() {
        const finish=state.reviewBusy || state.publicationBusy ? () => {} : beginButtonFeedback($('ontology-list-button'));
        try {
        const identityEpoch = state.identityEpoch;
        try {
          const payload = await apiRequest('/v1/ontologies?limit=100');
          if (identityEpoch !== state.identityEpoch) return;
          state.ontologies = Array.isArray(payload.items) ? payload.items : [];
          renderOntologies();
        } catch (error) {
          if (identityEpoch !== state.identityEpoch) return;
          elements.ontologyList.innerHTML = `<div class="output-box">${escapeHtml(error.message)}</div>`;
        }
        } finally {finish();}
      }

      function ontologySourceInput() {
        return {
          filename: state.ontologyFilename || 'ontology.yaml',
          content: elements.ontologyEditor.value,
          ...(state.ontologyRuleRegistry ? {rule_reference_registry: state.ontologyRuleRegistry} : {}),
        };
      }

      function ontologyInputMatches(validation) {
        return Boolean(validation && validation.identityEpoch === state.identityEpoch
          && validation.selection === state.ontologyFileSelection
          && validation.input === JSON.stringify(ontologySourceInput()));
      }

      function syncOntologyControls() {
        const busy = Boolean(state.ontologySaving);
        const pending = Boolean(state.ontologyFileLoading || state.ontologyRuleLoading || state.ontologyValidating);
        $('ontology-validate-button').disabled = busy || pending || Boolean(state.ontologyRuleError) || !elements.ontologyEditor.value.trim();
        $('ontology-import-button').disabled = busy || pending || !ontologyInputMatches(state.ontologyValidation);
        for (const id of ['ontology-file-button', 'ontology-file', 'ontology-rule-file-button', 'ontology-rule-file', 'ontology-clear']) $(id).disabled = busy;
        elements.ontologyEditor.readOnly = busy;
      }

      function setOntologyInputError(message = '') {
        state.ontologyInputError = message;
        $('ontology-input-error').textContent = message;
        $('ontology-input-error').hidden = !message;
        elements.ontologyEditor.setAttribute('aria-invalid', String(Boolean(message)));
        syncOntologyControls();
      }

      function invalidateOntologyValidation() {
        if (state.ontologyRuleLoading) {
          state.ontologyRuleRegistry = null;
          $('ontology-rule-file').value = '';
          $('ontology-rule-file-name').textContent = '';
        }
        state.ontologyFileSelection += 1;
        state.ontologyValidation = null;
        state.ontologyValidating = false;
        state.ontologyFileLoading = false;
        state.ontologyRuleLoading = false;
        $('ontology-validation').hidden = true;
        $('ontology-validation-summary').textContent = '';
        $('ontology-validation-counts').replaceChildren();
        $('ontology-diagnostics').replaceChildren();
        $('ontology-normalized-details').open = false;
        $('ontology-normalized-json').textContent = '';
        $('ontology-validation-status').textContent = elements.ontologyEditor.value.trim() ? '内容已变更，请重新校验后保存。' : '选择文件后自动开始校验。';
        setOntologyInputError(state.ontologyRuleError);
      }

      function clearOntologyInput() {
        elements.ontologyEditor.value = '';
        state.ontologyFilename = '';
        state.ontologyExpectedChecksum = null;
        state.ontologyRuleRegistry = null;
        state.ontologyRuleError = '';
        $('ontology-file').value = '';
        $('ontology-rule-file').value = '';
        $('ontology-rule-file-name').textContent = '';
        $('ontology-file-name').textContent = '尚未选择文件';
        $('ontology-source-details').open = false;
        invalidateOntologyValidation();
      }

      function renderOntologyValidation(payload) {
        const summary = payload.summary || {};
        const diagnostics = Array.isArray(payload.diagnostics) ? payload.diagnostics : [];
        const unsupportedFormat = !payload.valid && diagnostics.some(item => ['unsupported_ontology_format', 'unsupported_source_contract', 'unsupported_contract_version'].includes(item.code));
        const errors = diagnostics.filter(item => String(item.severity).toLowerCase() === 'error').length;
        const warnings = diagnostics.filter(item => String(item.severity).toLowerCase() === 'warning').length;
        $('ontology-validation').hidden = false;
        $('ontology-validation-badge').textContent = payload.valid ? '校验通过' : unsupportedFormat ? '格式不兼容' : '需要修正';
        $('ontology-validation-badge').className = `trust-badge ${payload.valid ? 'authoritative' : 'danger'}`;
        const version = summary.source_version ? `源版本 ${summary.source_version} · 系统版本 ${summary.version}` : summary.version != null ? `版本 ${summary.version}` : '';
        $('ontology-validation-summary').textContent = [summary.key, version, payload.valid ? `可保存为本体版本${warnings ? `，有 ${warnings} 项提示需核对` : ''}。` : unsupportedFormat ? '此文件格式尚未适配，请使用受支持的本体定义。' : `发现 ${errors || '若干'} 项错误，请修正源文件后重新校验。`].filter(Boolean).join(' · ');
        $('ontology-validation-counts').hidden = !payload.valid;
        $('ontology-validation-counts').innerHTML = [['实体类型',summary.entity_type_count],['关系类型',summary.relationship_type_count],['属性定义',summary.property_count]].map(([label,count]) => `<span>${label}<strong>${escapeHtml(number(count || 0))}</strong></span>`).join('');
        $('ontology-diagnostics').innerHTML = diagnostics.map(item => {
          const severity = String(item.severity || 'info').toLowerCase();
          const location = [item.path, item.line != null ? `第 ${item.line} 行${item.column != null ? `，第 ${item.column} 列` : ''}` : ''].filter(Boolean).join(' · ');
          return `<article class="ontology-diagnostic" data-severity="${escapeHtml(severity)}"><strong>${escapeHtml({error:'错误',warning:'提示',info:'说明'}[severity] || '说明')} · ${escapeHtml(item.message)}</strong>${location ? `<p>${escapeHtml(location)}</p>` : ''}${item.code ? `<small>${escapeHtml(item.code)}</small>` : ''}</article>`;
        }).join('');
        $('ontology-normalized-details').hidden = !payload.normalized_definition;
        $('ontology-normalized-json').textContent = payload.normalized_definition ? JSON.stringify(payload.normalized_definition, null, 2) : '';
        elements.ontologyEditor.setAttribute('aria-invalid', String(!payload.valid));
        $('ontology-validation-status').textContent = payload.valid ? '后台校验通过，可保存本体。' : unsupportedFormat ? '格式不兼容，尚未保存。' : '校验未通过，尚未保存。';
      }

      async function validateOntologyEditor() {
        if (state.ontologySaving || state.ontologyFileLoading || state.ontologyRuleLoading || state.ontologyRuleError) return;
        invalidateOntologyValidation();
        if (!elements.ontologyEditor.value.trim()) return;
        const identityEpoch = state.identityEpoch;
        const selection = state.ontologyFileSelection;
        const body = ontologySourceInput();
        const input = JSON.stringify(body);
        const current = () => identityEpoch === state.identityEpoch && selection === state.ontologyFileSelection && input === JSON.stringify(ontologySourceInput());
        state.ontologyValidating = true;
        $('ontology-validation-status').textContent = '后台正在解析、诊断并校验本体…';
        syncOntologyControls();
        try {
          const payload = await apiRequest('/v1/ontologies:validate', {method: 'POST', headers: {'Content-Type':'application/json'}, body: input});
          if (!current()) return;
          renderOntologyValidation(payload);
          if (payload.valid && payload.normalized_definition && payload.source_sha256 && payload.normalization_checksum) {
            state.ontologyValidation = {identityEpoch, selection, input, payload};
          }
        } catch (error) {
          if (!current()) return;
          setOntologyInputError(error.message);
          $('ontology-validation-status').textContent = '后台校验未完成，请重试。';
        } finally {
          if (current()) {
            state.ontologyValidating = false;
            syncOntologyControls();
          }
        }
      }

      async function importOntology() {
        if (state.ontologySaving || state.ontologyFileLoading || state.ontologyRuleLoading || state.ontologyValidating || !ontologyInputMatches(state.ontologyValidation)) return;
        const identityEpoch = state.identityEpoch;
        const verified = state.ontologyValidation;
        const body = {...JSON.parse(verified.input), expected_source_sha256: verified.payload.source_sha256, expected_normalization_checksum: verified.payload.normalization_checksum, activate: false};
        if (state.ontologyExpectedChecksum) body.expected_checksum = state.ontologyExpectedChecksum;
        state.ontologySaving = true;
        syncOntologyControls();
        $('ontology-validation-status').textContent = '正在保存校验通过的本体版本…';
        try {
          const payload = await apiRequest('/v1/ontologies:import-file', {method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body)});
          if (identityEpoch !== state.identityEpoch) return;
          state.ontologyValidation = null;
          state.ontologyExpectedChecksum = payload.checksum || null;
          $('ontology-validation-status').textContent = payload.status === 'PUBLISHED' ? '该版本已保存并处于启用状态。' : '本体版本已保存。启用后可用于实例提取。';
          showToast(`本体已保存：${payload.key} · v${payload.import_capabilities?.source_version || payload.source_contract?.metadata?.ontology_version || payload.version}`);
          await loadOntologies();
        } catch (error) {
          if (identityEpoch !== state.identityEpoch) return;
          setOntologyInputError(error.message);
          $('ontology-validation-status').textContent = '保存未完成，请核对错误后重试。';
        } finally {
          if (identityEpoch === state.identityEpoch) {
            state.ontologySaving = false;
            syncOntologyControls();
          }
        }
      }

      async function loadOntologyFile() {
        const file = $('ontology-file').files?.[0];
        if (!file || state.ontologySaving) return;
        clearOntologyInput();
        const identityEpoch = state.identityEpoch;
        const selection = state.ontologyFileSelection;
        const current = () => identityEpoch === state.identityEpoch && selection === state.ontologyFileSelection;
        state.ontologyFileLoading = true;
        state.ontologyFilename = file.name;
        $('ontology-file-name').textContent = file.name;
        $('ontology-validation-status').textContent = '正在读取本体文件…';
        syncOntologyControls();
        try {
          if (!/\.(yaml|yml|json)$/i.test(file.name)) throw new Error('请选择 YAML、YML 或 JSON 本体文件。');
          if (!file.size || file.size > MAX_ONTOLOGY_BYTES) throw new Error('本体文件必须在 1 byte 到 1 MB 之间。');
          const bytes = await file.arrayBuffer();
          if (!current()) return;
          let text;
          try { text = new TextDecoder('utf-8', {fatal: true, ignoreBOM: true}).decode(bytes); }
          catch (_) { throw new Error('本体文件必须使用 UTF-8 编码。'); }
          elements.ontologyEditor.value = text;
          elements.ontologyEditor.setSelectionRange?.(0, 0);
          elements.ontologyEditor.scrollTop = 0;
          state.ontologyFileLoading = false;
          await validateOntologyEditor();
        } catch (error) {
          if (current()) {
            state.ontologyFileLoading = false;
            setOntologyInputError(error.message);
            $('ontology-validation-status').textContent = '文件未载入，请检查后重新选择。';
          }
        }
      }

      async function loadOntologyRuleReferences() {
        const file = $('ontology-rule-file').files?.[0];
        if (!file || state.ontologySaving) return;
        state.ontologyRuleError = '';
        invalidateOntologyValidation();
        state.ontologyRuleRegistry = null;
        const identityEpoch = state.identityEpoch;
        const selection = state.ontologyFileSelection;
        const current = () => identityEpoch === state.identityEpoch && selection === state.ontologyFileSelection;
        state.ontologyRuleLoading = true;
        $('ontology-rule-file-name').textContent = file.name;
        syncOntologyControls();
        try {
          if (!file.size || file.size > MAX_ONTOLOGY_BYTES) throw new Error('规则引用目录不能超过 1 MB。');
          const text = await file.text();
          if (!current()) return;
          const value = JSON.parse(text);
          if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error('规则引用目录必须是 JSON 对象。');
          state.ontologyRuleRegistry = value;
          state.ontologyRuleLoading = false;
          await validateOntologyEditor();
        } catch (error) {
          if (current()) {
            state.ontologyRuleLoading = false;
            state.ontologyRuleError = error instanceof SyntaxError ? '规则引用目录不是有效的 JSON，请重新选择有效目录。' : error.message;
            setOntologyInputError(state.ontologyRuleError);
          }
        }
      }

      async function publishOntology(index) {
        const item = state.ontologies[index];
        if (!item) return;
        const identityEpoch = state.identityEpoch;
        try {
          const payload = await apiRequest(`/v1/ontologies/${encodeURIComponent(item.tbox_id)}:publish`, {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({expected_active_tbox_id: activeOntology(item.key)?.tbox_id || null}),
          });
          if (identityEpoch !== state.identityEpoch) return;
          refreshReviewResolutions(identityEpoch);
          $('document-tbox').value = payload.key || item.key;
          showToast(`本体已启用：${payload.key || item.key}`);
          await loadOntologies();
        } catch (error) {
          if (identityEpoch === state.identityEpoch) showToast(error.message);
        }
      }

      async function importABox() {
        if (state.expertImportBusy || state.expertPublishing) return;
        const identityEpoch = state.identityEpoch;
        state.expertImportBusy = true;
        invalidateExpertImportReceipt();
        $('abox-import-button').disabled = true;
        try {
          const draftText = elements.aboxEditor.value;
          const body = parseJsonEditor(elements.aboxEditor, '权威 A-Box');
          elements.aboxOutput.textContent = '';
          const payload = await apiRequest('/v1/knowledge/authoritative:import', {
            method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body),
          });
          if (identityEpoch !== state.identityEpoch) return;
          if (elements.aboxEditor.value !== draftText) {
            output(elements.aboxOutput, '本次导入已完成，但编辑器内容已改变。请核对当前草稿并重新导入，再发布。');
            return;
          }
          state.expertRevisionIds = [...new Set(payload.revision_ids || [])];
          state.expertImportMayReplace = [...(body.mentions || []), ...(body.assertions || [])].some(item => item.expected_previous_revision > 0);
          output(elements.aboxOutput, payload);
          $('abox-publication-panel').hidden = !state.expertRevisionIds.length;
          $('abox-publish-button').hidden = false;
          $('abox-next-button').hidden = true;
          $('abox-publication-note').textContent = `已导入 ${state.expertRevisionIds.length} 条专家记录，尚未发布。核对上方结果后，点击“发布专家基准”；只发布这次导入的记录。`;
          showToast('专家实例已导入。下一步在本区域发布专家基准。');
        } catch (error) {
          if (identityEpoch === state.identityEpoch) output(elements.aboxOutput, error.message);
        } finally {
          if (identityEpoch === state.identityEpoch) {state.expertImportBusy = false;refreshABoxPreparation();}
        }
      }

      function invalidateExpertImportReceipt() {
        state.expertRevisionIds = [];
        state.expertImportMayReplace = false;
        $('abox-publication-panel').hidden = true;
      }

      async function publishImportedABox() {
        if (state.expertPublishing || state.expertImportBusy || !state.expertRevisionIds.length) return;
        const identityEpoch = state.identityEpoch;
        const importedIds = [...state.expertRevisionIds];
        const mayReplace = state.expertImportMayReplace;
        state.expertPublishing = true;
        $('abox-publish-button').disabled = true;
        try {
          // Refresh only authorized metadata. The CAS below still detects any
          // publication change after these reads; no unrelated candidate is added.
          const [history, candidates] = await Promise.all([
            apiRequest('/v1/knowledge/publications?limit=100'),
            apiRequest('/v1/knowledge/publication-candidates?limit=100'),
          ]);
          if (identityEpoch !== state.identityEpoch) return;
          const active = (history.items || []).find(item => item.status === 'ACTIVE');
          const activeIds = new Set(active?.published_revision_ids || []);
          const revisionIds = importedIds.filter(id => !activeIds.has(id));
          const selected = (candidates.items || []).filter(item => revisionIds.includes(item.record?.revision_id));
          if (mayReplace && selected.length !== revisionIds.length) throw new Error('本次导入包含专家记录更新，但部分记录不在当前最多 100 条的可核对候选中。为避免遗漏替换，尚未提交发布。请先处理其他待发布记录，或缩小更新批次后重新导入。');
          // The list is bounded to 100; absence is not proof that an imported
          // revision is unpublishable. Publish only the exact import receipt and
          // let the backend validate every current revision and replacement.

          if (revisionIds.length) {
            const replaceRecordIds = [...new Set(selected.filter(item => item.requires_replacement).map(item => item.record.record_id))];
            await apiRequest('/v1/knowledge/publications:publish', {
              method: 'POST', headers: {'Content-Type':'application/json'},
              body: JSON.stringify({approved_revision_ids: revisionIds, expected_active_publication_id: active?.publication_id || null, remove_record_ids: [], replace_record_ids: replaceRecordIds}),
            });
          }
          if (identityEpoch !== state.identityEpoch) return;
          for (const id of importedIds) {
            state.approvedRevisions.delete(id);
            state.selectedCandidateRevisions.delete(id);
          }
          elements.publicationRevisions.value = elements.publicationRevisions.value.split(/\s+/).filter(id => id && !importedIds.includes(id)).join('\n');
          state.expertRevisionIds = [];
          $('abox-publication-note').textContent = `专家基准已发布：${importedIds.length} 条记录。可继续上传资料，构建更多实例。`;
          $('abox-publish-button').hidden = true;
          $('abox-next-button').hidden = false;
          invalidateInventory();
          refreshReviewResolutions(identityEpoch);
          showToast('专家实例已发布，可以继续上传资料。');
          await Promise.allSettled([loadPublicationCandidates(), loadHistory(), loadInventory(), loadQuality(), loadQualityHistory(), loadActiveDocuments()]);
        } catch (error) {
          if (identityEpoch === state.identityEpoch) $('abox-publication-note').textContent = error.message;
        } finally {
          if (identityEpoch === state.identityEpoch) {state.expertPublishing = false;$('abox-publish-button').disabled = false;}
        }
      }

      // Source type belongs to the upload draft, independently of page navigation.
      function clearConstructionResult() {
        state.uploadDraftEpoch = (state.uploadDraftEpoch || 0) + 1;
        state.constructionReceipt=null;
        elements.constructionOutput.textContent = '';
        elements.constructionOutput.hidden = true;
        elements.constructionOutput.classList.remove('upload-check');
        $('construction-validation-summary').innerHTML = '';
        $('construction-next').hidden = true;
        $('construction-next-note').textContent = '';
        state.lastConstructionMode = null;
        state.lastConstructionScope = null;
      }

      function resetUploadDraft() {
        clearConstructionResult();
        uploadMapping.clear({notify:false});
        for (const id of ['document-file', 'document-title', 'document-uri', 'document-asset']) $(id).value = '';
        $('document-chunk-max-chars').value = '1200';
        $('document-chunk-strategy').value = 'structure';
        updateChunkingSummary();
        $('document-file-name').textContent = '尚未选择文件';
        state.uploadAutoTitle = null;
        state.uploadAutoUri = null;
        state.uploadDraftCompleted = false;
        updateConstructionMode();
      }

      function selectUploadFile(file) {
        clearConstructionResult();
        $('document-file-name').textContent = file ? file.name : '尚未选择文件';
        updateConstructionMode();
        if (!file) return;
        const title = $('document-title'), uri = $('document-uri');
        // Follow a new file for generated metadata. A consumed draft must never
        // silently assign a different source to the previous document either.
        if (!title.value.trim() || title.value === state.uploadAutoTitle || state.uploadDraftCompleted) {
          title.value = file.name.replace(/\.[^.]+$/, '');
          state.uploadAutoTitle = title.value;
        }
        if (!uri.value.trim() || uri.value === state.uploadAutoUri || state.uploadDraftCompleted) {
          const safeName = encodeURIComponent(file.name.normalize('NFC')) || 'document';
          uri.value = defaultDocumentUri(safeName);
          state.uploadAutoUri = uri.value;
        }
        state.uploadDraftCompleted = false;
        updateChunkingSummary();
      }

      function setUploadKnowledgeScope(scope) {
        const select = $('document-knowledge-scope');
        if (state.constructionBusy) {
          select.value = state.uploadKnowledgeScope;
          showToast('请等待当前上传结束后再切换资料类型。');
          return false;
        }
        if (!['BUSINESS', 'AUTHORITATIVE'].includes(scope)) return false;
        if (scope !== state.uploadKnowledgeScope) resetUploadDraft();
        state.uploadKnowledgeScope = scope;
        select.value = scope;
        $('document-knowledge-scope-note').textContent = scope === 'AUTHORITATIVE'
          ? '请确认资料的权威依据；抽取候选仍需复核与发布。'
          : '业务资料形成次权威候选，复核与发布后可用于问答。';
        updateConstructionMode();
        return true;
      }

      function renderConstructionOntology() {
        const key = $('document-tbox').value.trim();
        const current = activeOntology(key);
        $('foundation-status').textContent = current ? `${current.key} · v${current.import_capabilities?.source_version || current.version}` : key ? `${key} · 未启用` : '尚未启用本体';
        $('foundation-status').title = $('foundation-status').textContent;
        $('inspect-build-ontology').textContent = current ? '查看' : '配置';
      }

      function renderBuildView() {
        const building = ['baseline', 'business'].includes(state.constructionFlow);
        const view = state.buildView || (state.ontologies.some(item => item.status === 'PUBLISHED') ? 'instances' : 'ontology');
        $('expert-foundation').hidden = !building || view !== 'ontology';
        $('instance-workspace').hidden = !building || view !== 'instances';
        for (const name of ['ontology', 'instances']) {
          const selected = name === view;
          $(`build-tab-${name}`).setAttribute('aria-selected', String(selected));
          $(`build-tab-${name}`).tabIndex = selected ? 0 : -1;
        }
      }

      function selectBuildView(view) {
        if (!['ontology', 'instances'].includes(view)) return;
        state.buildView = view;
        showConstructionFlow(lastBuildFlow);
      }

      function showConstructionFlow(flow, targetId = null) {
        if (!['baseline','business','browse','maintenance'].includes(flow)) return;
        // Legacy links still target the same controls, independent of source type.
        const aliases = {'business-upload-slot':'source-upload-slot', 'step-abox':'step-review', 'baseline-publication-slot':'step-publication'};
        targetId = aliases[targetId] || targetId;
        state.constructionFlow = flow;
        const building = flow === 'baseline' || flow === 'business';
        if (building) {
          lastBuildFlow = flow;
          if (targetId) state.buildView = targetId === 'step-ontology' ? 'ontology' : 'instances';
        }
        $('governance-build').hidden = !building;
        $('maintenance-flow-nav').hidden = flow !== 'maintenance';
        host.querySelectorAll('[data-construction-flow]').forEach(section => {
          section.hidden = section.dataset.constructionFlow !== (building ? 'build' : flow);
        });
        renderBuildView();
        if (building) {
          const steps = [['source-upload-slot','01','上传资料'],['step-review','02','复核候选'],['step-publication','03','发布知识']];
          $('construction-flow-steps').innerHTML = steps.map(([id,number,label])=>`<button class="button" type="button" data-step-target="${id}" aria-label="${number} ${label}" aria-controls="${id === 'source-upload-slot' ? 'upload-card' : id}"><span class="step-number" aria-hidden="true">${number}</span> <span>${label}</span></button>`).join('');
          $('construction-flow-steps').querySelectorAll('[data-step-target]').forEach(button=>button.addEventListener('click',()=>{
            showConstructionFlow(flow,button.dataset.stepTarget);
            // Rebuilt step controls retain keyboard focus.
            $('construction-flow-steps').querySelector(`[data-step-target="${button.dataset.stepTarget}"]`)?.focus();
          }));
          const step = targetId === 'step-review' ? 'review' : targetId === 'step-publication' ? 'publish' : targetId && targetId !== 'step-ontology' ? 'upload' : buildStep;
          buildStep=step;
          for(const [id,name] of [['upload-card','upload'],['step-review','review'],['step-publication','publish']]) $(id).hidden=name!==step;
          const stepIndex=['upload','review','publish'].indexOf(step);
          [...$('construction-flow-steps').children].forEach((button,index)=>{button.classList.toggle('selected',index===stepIndex);button.setAttribute('aria-current',index===stepIndex?'step':'false');});
          updateConstructionMode();
        } else if (flow==='maintenance') {
          const selected=targetId||'maintenance-quality';
          $('construction-flow-description').textContent='检查当前知识质量，追溯修订、来源与发布历史。';
          $('governance-maintenance').querySelectorAll(':scope > section').forEach(section=>section.hidden=section.id!==selected);
          $('maintenance-flow-steps').querySelectorAll('button').forEach(button=>button.classList.toggle('selected',button.dataset.flowTarget===selected));
        }
        onNavigate(flow==='browse'?'records':flow==='maintenance'?'maintenance':'build');
        if (flow==='browse') state.knowledgeBrowser?.activate();
        if (targetId === 'step-ontology') requestAnimationFrame(() => $('governance-build').scrollIntoView({behavior:'smooth',block:'start'}));
        if (targetId && !building) requestAnimationFrame(()=>$(targetId)?.scrollIntoView({behavior:'smooth',block:'start'}));
      }

      async function requirePublishedConstructionOntology(key, identityEpoch) {
        const payload = await apiRequest('/v1/ontologies?limit=100');
        if (identityEpoch !== state.identityEpoch) return false;
        state.ontologies = Array.isArray(payload.items) ? payload.items : [];
        renderOntologies();
        if (activeOntology(key)) return true;
        const draft = state.ontologies.some(item => item.key === key && item.status === 'DRAFT');
        $('construction-next').hidden = false;
        $('construction-next-note').textContent = draft
          ? `本体“${key}”尚未启用。请先在本体构建区保存版本并启用本体，然后回来上传。`
          : `尚未找到已发布本体“${key}”。请先在本体构建区导入并发布本体，或填写正确的已启用本体 key。`;
        $('construction-next-button').textContent = '前往本体构建';
        $('construction-next-button').onclick = () => showConstructionFlow('baseline', 'step-ontology');
        throw new Error($('construction-next-note').textContent);
      }

      function demoKit() {
        return state.bootstrap?.defaults?.industrial_demo || null;
      }

      function refreshABoxPreparation(message = null) {
        const empty = !elements.aboxEditor.value.trim();
        $('abox-prepare-button').hidden = !empty;
        $('abox-import-button').disabled = empty || Boolean(state.expertImportBusy);
        if (message) output(elements.aboxOutput, message);
      }

      function clearDemoSourceBinding() {
        if (state.demoSourceBinding) {
          elements.aboxEditor.value = '';
          refreshABoxPreparation('身份已切换；请在当前身份下重新准备权威来源与实例。');
        }
        state.demoSourceBinding = null;
      }

      function renderDemoKit() {
        const kit = demoKit();
        if (!kit) { $('demo-materials').hidden = true; return; }
        $('upload-demo-prepare').hidden = false;
        $('demo-files').innerHTML = kit.files.map(item => `<article class="governance-item"><div class="governance-item-head"><div><strong>${escapeHtml(item.label)}</strong><p>${escapeHtml(item.filename)} · ${escapeHtml(item.characters)} 字符 · ${escapeHtml(item.expected_chunks)} Chunk<br>${escapeHtml(item.metadata.title)}</p></div></div><div class="workbench-actions"><a class="button" href="/playground/demo-files/${encodeURIComponent(item.filename)}" download="${escapeHtml(item.filename)}">下载文件</a><button class="button" type="button" data-demo-prepare="${escapeHtml(item.id)}">${item.construction_mode === 'SOURCE_ONLY' ? '填写权威来源信息' : '填写业务资料信息'}</button></div></article>`).join('');
        $('demo-files').querySelectorAll('[data-demo-prepare]').forEach(button => {
          button.addEventListener('click', foregroundAction('', () => prepareDemoUpload(button.dataset.demoPrepare)));
        });
      }

      function prepareDemoUpload(id) {
        if (state.constructionBusy) { showToast('请等待当前构建任务结束。'); return; }
        const item = demoKit()?.files.find(file => file.id === id);
        if (!item) return;
        setUploadKnowledgeScope(item.id === 'authoritative_source' ? 'AUTHORITATIVE' : 'BUSINESS');
        showConstructionFlow('business', 'source-upload-slot');
        for (const [field, value] of Object.entries({title: item.metadata.title, uri: item.metadata.canonical_uri, source: item.metadata.source_name, language: item.metadata.language, tbox: demoKit().ontology_key})) {
          $(`document-${field}`).value = value;
        }
        $('document-file').value = '';
        $('document-file-name').textContent = '尚未选择文件';
        renderConstructionOntology();
        $('construction-next').hidden = true;
        $('document-extraction-mode').value = 'LLM';
        updateConstructionMode();
        output($('demo-note'), `已填写 ${item.label} 的元数据。请在上传步骤选择下载的 ${item.filename}，确认访问组后提交。`);
        $('instance-workspace').scrollIntoView({behavior: 'smooth', block: 'start'});
      }

      function refreshManualForm() {
        const tbox = activeOntology($('document-tbox').value.trim());
        const kind = $('manual-kind').value;
        const types = tbox?.entity_types || [];
        for (const id of ['manual-subject-type', 'manual-object-type']) {
          const previous = $(id).value;
          $(id).innerHTML = types.map(x => `<option value="${escapeHtml(x.name)}">${escapeHtml(x.name)}</option>`).join('');
          if (types.some(x => x.name === previous)) $(id).value = previous;
        }
        const names = kind === 'PROPERTY'
          ? (types.find(x => x.name === $('manual-subject-type').value)?.properties || [])
          : (tbox?.relationship_types || []).filter(x => x.source_types.includes($('manual-subject-type').value));
        $('manual-predicate').innerHTML = names.map(x => `<option value="${escapeHtml(x.name)}">${escapeHtml(x.name)}</option>`).join('');
        host.querySelectorAll('[data-manual-fact-field]').forEach(x => x.hidden = kind === 'ENTITY');
        host.querySelectorAll('[data-manual-property-field]').forEach(x => x.hidden = kind !== 'PROPERTY');
        host.querySelectorAll('[data-manual-relation-field]').forEach(x => x.hidden = kind !== 'RELATIONSHIP');
      }

      async function submitManualFact() {
        if (state.manualBusy || state.constructionBusy) return;
        const identityEpoch = state.identityEpoch;
        state.manualBusy = true;
        $('manual-save-button').disabled = true;
        try {
          const kind = $('manual-kind').value;
          const fact = {kind, subject: {entity_type: $('manual-subject-type').value, canonical_name: $('manual-subject-name').value.trim()}};
          if (kind !== 'ENTITY') fact.predicate = $('manual-predicate').value;
          if (kind === 'RELATIONSHIP') fact.object_entity = {entity_type: $('manual-object-type').value, canonical_name: $('manual-object-name').value.trim()};
          if (kind === 'PROPERTY') {
            fact.literal = {raw_literal: $('manual-value').value.trim()};
            if ($('manual-unit').value.trim()) fact.literal.raw_unit = $('manual-unit').value.trim();
          }
          const tboxKey = $('document-tbox').value.trim();
          const groups = selectedDocumentAccessGroups();
          if (!fact.subject.canonical_name || !groups.length) throw new Error('请填写实体名称并选择访问组');
          if (!await requirePublishedConstructionOntology(tboxKey, identityEpoch)) return;
          const fingerprint = JSON.stringify({fact, tboxKey, groups, identityEpoch});
          if (state.manualOperation?.fingerprint !== fingerprint) state.manualOperation = {fingerprint, key: crypto.randomUUID()};
          const operationKey = state.manualOperation.key;
          const payload = await apiRequest('/v1/knowledge:construct', {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({operation_key: operationKey, canonical_uri: `urn:graphrag:human:${operationKey}`,
              title: '人工补充', source_name: '人工补充记录', mime_type: 'text/plain', language: 'zh',
              tbox_key: tboxKey, access_groups: groups, extraction_mode: 'MANUAL', manual_fact: fact}),
          });
          if (identityEpoch !== state.identityEpoch) return;
          const count = payload.chunks.reduce((n, x) => n + (x.mention_record_ids?.length || 0) + (x.assertion_record_ids?.length || 0), 0);
          output($('manual-output'), count ? `已保存 ${count} 条人工补充记录，请在下方确认和发布。` : `本体或数据校验未通过，未产生实例。请检查类型、属性值和单位。${payload.chunks.flatMap(x => x.finding_codes || []).join('、')}`);
          await loadReviews({refreshResolutions:true});
        } catch (error) {
          if (identityEpoch === state.identityEpoch) output($('manual-output'), error.message);
        } finally {
          if (identityEpoch === state.identityEpoch) {state.manualBusy = false;$('manual-save-button').disabled = false;}
        }
      }

      function updateConstructionMode() {
        const mode = $('document-extraction-mode');
        const file = $('document-file').files?.[0];
        const xml = String(file?.name || '').toLowerCase().endsWith('.xml');
        mode.disabled = state.constructionBusy;
        $('document-format-note').hidden = !xml;
        $('document-format-note').textContent = xml
          ? 'XML 会先校验格式，再按所选方式处理。自动构建按本体抽取对象、属性和有明确证据的关系；不把组态信息当作实测数据。' : '';
        const sourceOnly = $('document-extraction-mode').value === 'SOURCE_ONLY';
        const mapped=uploadMapping.active;
        uploadMapping.sync();
        for(const id of ['document-chunk-max-chars','document-chunk-strategy']) $(id).disabled=state.constructionBusy||mapped;
        $('document-chunk-max-chars').closest('.field-grid').hidden=mapped;
        const chunkNote=$('document-chunking-note');
        chunkNote.dataset.defaultNote ||= chunkNote.textContent;
        chunkNote.textContent=mapped?`当前映射的切片设置：${chunkingDescription(uploadMapping.chunking())}。清除映射后可自行调整。`:chunkNote.dataset.defaultNote;
        $('construct-button').disabled=state.constructionBusy||uploadMapping.pending;
        $('preview-chunks-button').disabled=state.constructionBusy||uploadMapping.pending;
        $('document-knowledge-scope-note').textContent = sourceOnly
          ? ($('document-knowledge-scope').value === 'AUTHORITATIVE'
            ? '记录为用户声明的权威来源；保存不代表平台已核验，也不生成或发布知识。'
            : '仅保存业务来源和证据，尚未形成候选知识。')
          : ($('document-knowledge-scope').value === 'AUTHORITATIVE'
            ? '请确认资料的权威依据；抽取候选仍需复核与发布。'
            : '业务资料形成次权威候选，复核与发布后可用于问答。');
        $('provider-note').hidden = sourceOnly||mapped;
        const limits = state.bootstrap?.capabilities?.construction_limits || {};
        const feedbackEnabled = limits.max_validation_attempts === 2;
        const chunkLimit = sourceOnly ? limits.max_chunks : (limits.max_llm_chunks ?? limits.max_chunks);
        const boundedChunks = Number.isInteger(chunkLimit) && chunkLimit > 0
          ? `当前模式每份文档最多 ${chunkLimit} Chunks。` : '分块数量由服务端限制。';
        $('construct-button').textContent = sourceOnly
          ? '上传并保存来源' : '上传并构建';
        if (!state.constructionBusy) $('construction-submit-note').textContent = sourceOnly
          ? (xml ? '当前选择仅保存 XML 来源，不执行抽取；生成候选请选择“按本体抽取”。' : '仅保存来源，不生成候选知识。')
          : mapped?'按已加载映射构建候选，不调用提取模型；核对后再发布。':'构建后自动预审；疑难项先复核，再发布。';
        $('construction-mode-note').textContent = sourceOnly
          ? `${boundedChunks}仅切块与向量化，不执行 LLM 抽取或自动纠正。`
          : mapped?'程序按版本化映射读取原文并校验本体与证据；提取模型调用为 0。未解析的引用保留待补，候选仍需复核与发布。'
          : `${boundedChunks}结构化 JSON 自动生成字段映射，由程序构建记录；其他资料按片段抽取。${feedbackEnabled ? '映射或抽取校验失败后，最多自动纠正一次；模型服务错误或超时不自动重试。' : '当前配置不自动纠正校验失败。'}构建后自动预审，明确项自动确认，疑难项交给人工；确认后仍需发布。`;
      }

      function selectedChunking() {
        const mapped=uploadMapping.chunking();
        if(mapped)return mapped;
        const maxChars = Number($('document-chunk-max-chars').value);
        const strategy = $('document-chunk-strategy').value;
        if (![800,1200,2000,3000].includes(maxChars) || !['structure','fixed'].includes(strategy)) throw new Error('请选择有效的切片设置');
        return {max_chars:maxChars,strategy};
      }

      function chunkingDescription(value) {
        if(value?.strategy==='mapped_document'&&value.max_chars===65536)return 'XML 完整结构证据 · 最多 65,536 字符 · 程序映射不调用提取模型';
        if (!value || !Number.isInteger(value.max_chars) || !['structure','fixed'].includes(value.strategy)) return '';
        return `${value.strategy === 'structure' ? '文档结构优先' : '固定长度'} · 每片上限 ${number(value.max_chars)} 字符`;
      }

      function updateChunkingSummary() {
        $('document-chunking-summary').textContent = `切片：${chunkingDescription(selectedChunking())}`;
      }

      function chunkingMarkup(value, {preview=false}={}) {
        const description = chunkingDescription(value);
        if (!description) return '';
        const count = Number.isInteger(value.chunk_count) ? value.chunk_count : null;
        const range = Number.isInteger(value.min_chars) && Number.isInteger(value.max_actual_chars)
          ? `每片 ${number(value.min_chars)}～${number(value.max_actual_chars)} 字符` : '';
        const average = count > 0 && Number.isInteger(value.total_chars) ? `平均 ${number(Math.round(value.total_chars/count))} 字符` : '';
        return `<div class="chunking-preview" data-chunking-summary><strong>${preview ? '切片预览' : '本次切片'}${count === null ? '' : `：${count} 个片段`}</strong><p>${escapeHtml(description)}</p>${range ? `<p>${escapeHtml([range,average].filter(Boolean).join(' · '))}</p>` : ''}</div>`;
      }

      function autoReviewSummary(value) {
        if (!value || !['RUNNING','COMPLETED','PARTIAL','FAILED','SKIPPED'].includes(value.status)) return null;
        const text = (value, limit=2000) => typeof value === 'string' ? value.slice(0,limit) : '';
        const count = value => Number.isSafeInteger(value) && value >= 0 ? value : 0;
        const counts = Object.fromEntries(['entity_groups','approved_groups','approved_mentions','approved_assertions',
          'manual_groups','manual_assertions','blocked_assertions','incomplete'].map(key=>[key,count(value.counts?.[key])]));
        const sourceItems = Array.isArray(value.items) ? value.items.slice().sort((a,b)=>Number(a.decision==='AUTO_APPROVED')-Number(b.decision==='AUTO_APPROVED')) : [];
        const sourceIssues=Array.isArray(value.issues)?value.issues:[];
        const ids=values=>(Array.isArray(values)?values:[]).slice(0,200).map(id=>text(id,256));
        const issues=values=>(Array.isArray(values)?values:[]).slice(0,100).filter(issue=>issue?.code==='PROPERTY_REQUIRED' || /^CONTEXT_[A-Z0-9_]+$/.test(issue?.code || '')).map(issue=>({
          code:text(issue.code,128),property_name:text(issue.property_name,128),entity_count:count(issue.entity_count),
          entity_ids:ids(issue.entity_ids),record_ids:ids(issue.record_ids),reason:text(issue.reason),
          source_paths:(Array.isArray(issue.source_paths)?issue.source_paths:[]).slice(0,32).map(path=>text(path,1000))}));
        const context=value.context_mapping;
        const contextMapping=context && ['RUNNING','COMPLETED','PARTIAL','UNAVAILABLE','SKIPPED'].includes(context.status)?{
          status:context.status,added_assertions:count(context.added_assertions),applied:count(context.applied),
          uncertain:count(context.uncertain),overridden:count(context.overridden),issues:issues(context.issues),
          rules:(Array.isArray(context.rules)?context.rules:[]).slice(0,64).map(rule=>({
            source_path:text(rule.source_path,1000),target_collection:text(rule.target_collection,1000),property_name:text(rule.property_name,128),
            scope_path:typeof rule.scope_path==='string'?text(rule.scope_path,1000):null,binding_mode:text(rule.binding_mode,128),status:text(rule.status,128),
            applied:count(rule.applied),uncertain:count(rule.uncertain),overridden:count(rule.overridden),reason:text(rule.reason)}))}:null;
        return {job_id:text(value.job_id,256),run_id:text(value.run_id,256),status:value.status,
          stage:['CONTEXT','IDENTITY','FACTS','DONE'].includes(value.stage)?value.stage:'IDENTITY',
          policy_version:text(value.policy_version,256),initiated_by:text(value.initiated_by,256),reviewed_by:text(value.reviewed_by,256),
          counts,model_calls:count(value.model_calls),updated_at:text(value.updated_at,64),
          truncated:Boolean(value.truncated || sourceItems.length>100 || sourceIssues.length>100),
          issues:issues(sourceIssues),context_mapping:contextMapping,
          items:sourceItems.slice(0,100).map(item=>({record_id:text(item.record_id,256),
            input_revision:count(item.input_revision) || null,
            record_kind:item.record_kind==='ENTITY_MENTION'?'ENTITY_MENTION':'ASSERTION',
            decision:['AUTO_APPROVED','NEEDS_HUMAN','BLOCKED','INCOMPLETE'].includes(item.decision)?item.decision:'INCOMPLETE',
            reason_code:text(item.reason_code,128),reason:text(item.reason),target_entity_id:text(item.target_entity_id,256),
            evidence_ids:(Array.isArray(item.evidence_ids)?item.evidence_ids:[]).slice(0,32).map(id=>text(id,256))}))};
      }

      function autoReviewIssues(value) {
        const entries=[...(value?.issues || []),...(value?.context_mapping?.issues || [])];
        return [...new Map(entries.map(issue=>[JSON.stringify([issue.code,issue.property_name,issue.entity_ids,issue.source_paths]),issue])).values()];
      }

      function autoReviewStageLabel(value) {
        if(value.status==='RUNNING' && value.stage==='CONTEXT')return '正在核对文档上下文与适用范围';
        if (value.status==='RUNNING') return value.stage==='FACTS'?'正在审核属性与关系':'正在核对实体身份';
        if(value.status==='COMPLETED' && autoReviewIssues(value).length)return '自动审核完成，发布前仍需处理映射缺口';
        return ({COMPLETED:'自动预审完成',PARTIAL:'自动预审部分完成',FAILED:'自动预审未完成',SKIPPED:'本次未执行自动预审'})[value.status] || '等待自动预审';
      }

      function autoReviewGapMarkup(value) {
        const issues=autoReviewIssues(value);
        if(!issues.length)return ['PARTIAL','UNAVAILABLE'].includes(value.context_mapping?.status)
          ? '<div class="auto-review-gaps" data-auto-review-gaps><strong>部分资料尚未完成核对</strong><p>请查看预审依据并重试未完成项。</p></div>' : '';
        const affected=new Set(issues.flatMap(issue=>issue.entity_ids)).size;
        const incomplete=issues.some(issue=>issue.entity_count>issue.entity_ids.length);
        const instruction=value.status==='RUNNING'
          ? '自动预审仍在进行，缺口会随处理进度更新，请以最终结果为准。'
          : '可先补全有明确适用范围的上下文字段并重新审核；仍有冲突时，请修正来源映射或资料后重新构建。此处的缺口不能通过确认已有记录来补齐。';
        return `<div class="auto-review-gaps" data-auto-review-gaps><strong>待补字段 / 映射缺口 ${issues.length} 项（影响${incomplete?'至少 ':''}${affected} 个实体）</strong><p>${instruction}</p><p>未入图字段不能默认对所有实体生效；下列同名上下文路径仅供核对，不代表已经建立属性。</p>${issues.map((issue,index)=>`<details class="auto-review-gap-details"><summary>${escapeHtml(issue.property_name)} · 影响 ${issue.entity_count} 个实体</summary><p>${escapeHtml(issue.reason || '实体缺少本体要求的必填属性，需要核对原文及适用范围。')}</p>${issue.source_paths.length?`<p>待核对的上下文路径：${issue.source_paths.map(path=>`<code>${escapeHtml(path)}</code>`).join('、')}</p>`:'<p>没有提供可用的同名上下文路径，请核对来源资料。</p>'}<div class="auto-review-items">${issue.record_ids.slice(0,100).map((recordId,position)=>`<article class="auto-review-item" data-auto-review-item="gap-${index}-${position}"><small>相关实体 ${escapeHtml(shortId(issue.entity_ids[position] || ''))}</small><div class="workbench-actions"><button class="button" type="button" data-auto-review-evidence="${escapeHtml(recordId)}">查看相关实体与原文</button></div><div data-auto-review-evidence-panel hidden></div></article>`).join('')}</div>${issue.entity_count>Math.min(100,issue.record_ids.length)?'<p class="field-note">此处仅展开部分代表记录，影响实体总数以上方统计为准。</p>':''}</details>`).join('')}<div class="workbench-actions"><button class="button" type="button" data-auto-review-upload>返回上传资料</button></div></div>`;
      }

      function contextMappingMarkup(value) {
        const mapping=value.context_mapping;
        if(!mapping)return '';
        const labels={RUNNING:'处理中',COMPLETED:'已完成',PARTIAL:'部分完成',UNAVAILABLE:'暂不可用',SKIPPED:'未执行',APPLIED:'已应用',UNCERTAIN:'待核对',OVERRIDDEN:'保留局部值'};
        const bindingLabels={IDENTITY_TEMPLATE:'来源身份模板',ANCESTOR_DEFAULT:'上级作用域默认值'};
        return `<section class="context-mapping-summary" data-context-mapping-status="${mapping.status}"><strong>文档上下文补全 · ${labels[mapping.status]}</strong><p>新增 ${mapping.added_assertions} 条属性 · 已应用 ${mapping.applied} 项 · 待核对 ${mapping.uncertain} 项 · 保留局部值 ${mapping.overridden} 项</p><details><summary>查看上下文映射与适用范围（${mapping.rules.length}）</summary><p class="field-note">补全结果单独保留审计，原构建映射保持不变。局部字段与上下文字段分别核对。</p>${mapping.rules.map(rule=>`<article class="exact-evidence"><strong>${escapeHtml(rule.source_path)} → ${escapeHtml(rule.target_collection)} · ${escapeHtml(rule.property_name)}</strong><p>作用域：${escapeHtml(rule.scope_path===''?'文档根节点':rule.scope_path || '未确认')} · 绑定方式：${escapeHtml(bindingLabels[rule.binding_mode] || rule.binding_mode || '未确认')}</p><p>${escapeHtml(labels[rule.status] || rule.status)} · 已应用 ${rule.applied} · 待核对 ${rule.uncertain} · 保留局部值 ${rule.overridden}</p><p>${escapeHtml(rule.reason)}</p></article>`).join('') || '<p>当前没有可展示的上下文映射。</p>'}</details></section>`;
      }

      function hasContextRepairGap(value) {
        return ['PARTIAL','UNAVAILABLE'].includes(value?.context_mapping?.status) || [...(value?.issues || []),...(value?.context_mapping?.issues || [])].some(issue=>issue.code==='PROPERTY_REQUIRED' || /^CONTEXT_/.test(issue.code));
      }

      function autoReviewMarkup(value, {compact=false}={}) {
        if (!value) return '';
        const c=value.counts;
        const labels={AUTO_APPROVED:'自动通过',NEEDS_HUMAN:'需要人工判断',BLOCKED:'等待身份确认',INCOMPLETE:'尚未完成'};
        const status=value.status==='RUNNING'?'预审完成后会更新人工待办。':value.status==='SKIPPED'
          ? '本次记录保留现有审核状态，请查看任务明细。'
          : c.approved_mentions+c.approved_assertions>0?'明确项已自动确认；以下人工队列只保留未决记录。自动确认不等于发布。'
          : '预审尚无自动确认记录；未决记录仍可人工处理。确认后仍需发布。';
        const details=compact?'':`<details class="auto-review-details"><summary>查看自动预审依据与记录（${value.items.length}${value.truncated?'＋':''}）</summary><p class="field-note">以下是本次预审记录；后续人工修订以当前记录为准。模型审核调用 ${value.model_calls} 次。</p>${contextMappingMarkup(value)}<div class="auto-review-items">${value.items.map((item,index)=>`<article class="auto-review-item" data-auto-review-item="${index}"><div><strong>${escapeHtml(labels[item.decision])}</strong> · ${item.record_kind==='ENTITY_MENTION'?'实体提及':'属性或关系'}</div><p>${escapeHtml(item.reason || '请查看当前记录和原文依据。')}</p><small>记录 ${escapeHtml(shortId(item.record_id))}${item.target_entity_id?` · 实体 ${escapeHtml(shortId(item.target_entity_id))}`:''}</small><div class="workbench-actions"><button class="button" type="button" data-auto-review-evidence="${escapeHtml(item.record_id)}">查看原文与当前记录</button>${item.decision==='AUTO_APPROVED'?`<button class="button" type="button" data-auto-review-correct="${escapeHtml(item.record_id)}">转人工核查</button>`:''}</div><div data-auto-review-evidence-panel hidden></div></article>`).join('') || '<p>当前尚无已保存的逐条审核结果。</p>'}</div>${value.truncated?'<p class="field-note">此处仅展示部分审核依据；全部未决记录仍保留在人工队列中。</p>':''}<p class="field-note">审核策略 ${escapeHtml(value.policy_version)} · 审核服务 ${escapeHtml(value.reviewed_by)} · 更新 ${escapeHtml(value.updated_at)}</p></details>`;
        return `<section class="auto-review-summary" data-auto-review-status="${value.status}" data-auto-review-job="${escapeHtml(value.job_id)}"><strong>${escapeHtml(autoReviewStageLabel(value))}</strong><div class="auto-review-counts"><p>自动通过 <b>${c.approved_groups}</b> 个实体组 · <b>${c.approved_mentions}</b> 条提及 · <b>${c.approved_assertions}</b> 条事实</p><p>人工判断 <b>${c.manual_groups}</b> 个实体组 · <b>${c.manual_assertions}</b> 条事实</p><p>等待身份确认 <b>${c.blocked_assertions}</b> 条事实 · 尚未完成 <b>${c.incomplete}</b> 项</p></div><p class="field-note">${status}</p>${autoReviewGapMarkup(value)}${details}<div class="workbench-actions">${hasContextRepairGap(value) && value.status!=='RUNNING'?`<button class="button primary" type="button" data-auto-review-retry="${escapeHtml(value.job_id)}" ${state.autoReviewBusy?'disabled':''}>补全上下文并审核</button>`:(['FAILED','PARTIAL'].includes(value.status) || value.status!=='RUNNING' && (c.blocked_assertions+c.manual_groups+c.manual_assertions>0))?`<button class="button" type="button" data-auto-review-retry="${escapeHtml(value.job_id)}" ${state.autoReviewBusy?'disabled':''}>${c.incomplete>0 || ['FAILED','PARTIAL'].includes(value.status)?'重试未完成的预审':'续审未决记录'}</button>`:''}${compact?'<button class="button" type="button" data-auto-review-open>查看预审依据</button>':''}${c.approved_mentions+c.approved_assertions>0?'<button class="button" type="button" data-auto-review-publication>查看已确认内容</button>':''}</div></section>`;
      }

      function rememberAutoReview(value) {
        const summary=autoReviewSummary(value);
        if (!summary) return null;
        state.latestAutoReview=summary;
        const panel=$('review-auto-summary');
        if (panel) {panel.hidden=false;panel.innerHTML=autoReviewMarkup(summary,{compact:true});bindAutoReviewActions(panel);}
        elements.reviewList?.querySelectorAll?.('[data-auto-review-reason]').forEach(note=>{
          const item=state.reviews?.find(item=>item.record_id===note.dataset.autoReviewReason);
          note.innerHTML=item?autoReviewReasonMarkup(item):'';
        });
        if(elements.reviewList && Array.isArray(state.reviews) && !state.reviews.length && !state.reviewLoading)renderReviews();
        return summary;
      }

      function constructionJobDetail(jobId) {
        // Share only simultaneous reads within one identity and review generation.
        // Terminal repair increments its generation before requiring a fresh detail.
        const requests=state.constructionDetailRequests ||= new Map();
        const key=JSON.stringify([state.identityEpoch,state.autoReviewEpoch || 0,jobId]);
        if(requests.has(key))return requests.get(key);
        const request=Promise.resolve(apiRequest(`/v1/knowledge/construction-jobs/${encodeURIComponent(jobId)}`,{feedback:false}))
          .finally(()=>{if(requests.get(key)===request)requests.delete(key);});
        requests.set(key,request);
        return request;
      }

      async function refreshConstructionReceipt(jobId,{renderReceipt=false}={}) {
        if(!jobId)return null;
        const identity=state.identityEpoch,epoch=state.autoReviewEpoch || 0;
        const payload=await constructionJobDetail(jobId);
        if(identity!==state.identityEpoch || epoch!==(state.autoReviewEpoch || 0))return null;
        if(payload.job_id!==jobId)throw new Error('构建任务已变化，请刷新任务列表。');
        if(renderReceipt && JSON.stringify(state.constructionReceipt)!==JSON.stringify(payload))showConstructionResult(payload);
        else {
          rememberAutoReview(payload.auto_review);
          if(renderReceipt)renderConstructionNext(payload);
        }
        return payload;
      }

      function bindAutoReviewActions(container) {
        if (!container?.querySelectorAll) return;
        container.querySelectorAll('[data-auto-review-upload]').forEach(button=>button.addEventListener('click',()=>showConstructionFlow('business','source-upload-slot')));
        container.querySelectorAll('[data-auto-review-publication]').forEach(button=>button.addEventListener('click',foregroundAction('',async()=>{
          showConstructionFlow('business','step-publication');await loadPublicationCandidates();
        })));
        container.querySelectorAll('[data-auto-review-open]').forEach(button=>button.addEventListener('click',foregroundAction('',async()=>{
          const jobId=state.latestAutoReview?.job_id;if(!jobId)return;
          const identity=state.identityEpoch,epoch=state.autoReviewEpoch || 0;
          const payload=await constructionJobDetail(jobId);
          if(identity!==state.identityEpoch || epoch!==(state.autoReviewEpoch || 0))return;
          showConstructionFlow('business','source-upload-slot');showConstructionResult(payload);
          const details=elements.constructionOutput.querySelector('.auto-review-details');
          if(details){details.open=true;details.scrollIntoView({block:'start',behavior:'smooth'});}
        })));
        container.querySelectorAll('[data-auto-review-retry]').forEach(button=>button.addEventListener('click',foregroundAction('',()=>retryAutoReview(button.dataset.autoReviewRetry))));
        container.querySelectorAll('[data-auto-review-evidence]').forEach(button=>button.addEventListener('click',foregroundAction('',()=>loadAutoReviewEvidence(button))));
        container.querySelectorAll('[data-auto-review-correct]').forEach(button=>button.addEventListener('click',foregroundAction('',()=>correctPublishedRecord(button.dataset.autoReviewCorrect))));
      }

      async function loadAutoReviewEvidence(button) {
        const panel=button.closest('[data-auto-review-item]').querySelector('[data-auto-review-evidence-panel]');
        const identity=state.identityEpoch;
        panel.hidden=false;panel.textContent='正在读取当前记录…';
        try {
          const payload=await apiRequest(`/v1/knowledge/records/${encodeURIComponent(button.dataset.autoReviewEvidence)}/revisions?limit=1`);
          if(identity!==state.identityEpoch || panel.isConnected===false)return;
          const record=payload.items?.[0];if(!record)throw new Error('当前没有可访问的记录。');
          const status=({APPROVED:'已确认',PUBLISHED:'已发布',CANDIDATE:'待审核',QUARANTINED:'已暂缓',REJECTED:'未采纳'})[record.trust?.status] || record.trust?.status || '';
          panel.innerHTML=`<p>当前记录：${escapeHtml(reviewEntity(record).canonical_name || '未命名实体')} · ${escapeHtml(status)} · 修订 ${escapeHtml(record.revision)}</p>${reviewEvidence(record.evidence,'查看文档原文',record)}`;
          bindEvidenceActions(panel);
        } catch(error) {if(identity===state.identityEpoch && panel.isConnected!==false)panel.textContent=error.message;}
      }

      async function retryAutoReview(jobId, resumeIdentityIds=[]) {
        if(state.autoReviewBusy || state.constructionBusy || state.reviewBusy || state.publicationBusy)return;
        const identity=state.identityEpoch;
        const previous=state.latestAutoReview;
        state.autoReviewBusy=true;
        state.autoReviewEpoch=(state.autoReviewEpoch || 0)+1;
        const stop=watchAutoReviewProgress(jobId,identity);
        try {
          const result=await apiRequest(`/v1/knowledge/construction-jobs/${encodeURIComponent(jobId)}/auto-review:run`,{
            method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({retry:true,
              ...(resumeIdentityIds.length?{resume_identity_record_ids:resumeIdentityIds}:{})})});
          if(identity!==state.identityEpoch)return;
          // The operation response is final. Older progress reads must not replace it
          // while the independent human/publication queues are still refreshing.
          stop();
          state.autoReviewEpoch=(state.autoReviewEpoch || 0)+1;
          rememberAutoReview(result);
          if(state.constructionReceipt?.job_id===jobId)showConstructionResult({...state.constructionReceipt,auto_review:result});
          await refreshConstructionReceipt(jobId,{renderReceipt:state.constructionReceipt?.job_id===jobId});
          if(identity!==state.identityEpoch)return;
          await Promise.allSettled([loadReviews({refreshResolutions:true,refreshAutoReview:false}),loadPublicationCandidates()]);
          if(identity===state.identityEpoch)showToast(autoReviewStageLabel(result));
        } catch(error) {
          if(identity===state.identityEpoch){
            if(state.latestAutoReview?.status==='RUNNING')rememberAutoReview(previous);
            showToast(error.message);
          }
        } finally {
          stop();
          if(identity===state.identityEpoch){
            state.autoReviewBusy=false;rememberAutoReview(state.latestAutoReview);
            if(state.constructionReceipt?.job_id===jobId)showConstructionResult({...state.constructionReceipt,auto_review:state.latestAutoReview});
          }
        }
      }

      function watchAutoReviewProgress(jobId,identityEpoch) {
        let stopped=false,timer=null,currentRunObserved=false;
        const previous=state.latestAutoReview?.job_id===jobId?state.latestAutoReview:null;
        const preparing=hasContextRepairGap(previous)?'正在准备上下文补全…':'正在准备自动预审…';
        const display=$('construction-progress');display.hidden=false;display.textContent=preparing;
        const poll=async()=>{
          try {
            const value=await apiRequest(`/v1/knowledge/construction-jobs/${encodeURIComponent(jobId)}/auto-review`,{feedback:false});
            if(stopped || identityEpoch!==state.identityEpoch)return;
            const candidate=autoReviewSummary(value);
            if(!candidate || candidate.job_id!==jobId)return;
            const sameSavedVersion=previous && candidate.run_id===previous.run_id && candidate.updated_at===previous.updated_at;
            const before=Date.parse(previous?.updated_at || ''),after=Date.parse(candidate.updated_at);
            const newer=previous && (Number.isFinite(before) && Number.isFinite(after)
              ? after>before : candidate.updated_at && candidate.updated_at!==previous.updated_at);
            // The POST can spend time loading its source before creating RUNNING.
            // Compare saved server versions, never the client's wall clock.
            if(!currentRunObserved)currentRunObserved=Boolean(newer || candidate.status==='RUNNING' && (!previous || previous.status!=='RUNNING' || !sameSavedVersion));
            if(!currentRunObserved || sameSavedVersion && candidate.status!=='RUNNING'){
              if(!currentRunObserved)display.textContent=preparing;
              return;
            }
            const summary=rememberAutoReview(value);
            if(summary)display.textContent=autoReviewStageLabel(summary);
          } catch(_) {if(!stopped && identityEpoch===state.identityEpoch)display.textContent='预审进度暂时无法刷新，正在等待处理结果。';}
          finally {if(!stopped && identityEpoch===state.identityEpoch)timer=setTimeout(poll,3000);}
        };
        timer=setTimeout(poll,1000);
        return ()=>{stopped=true;clearTimeout(timer);if(identityEpoch===state.identityEpoch){display.hidden=true;display.textContent='';}};
      }

      function renderConstructionNext(payload) {
        const hasCandidates = (payload.chunks || []).some(chunk => chunk.mention_record_ids?.length || chunk.assertion_record_ids?.length);
        const rejectedChunks = (payload.chunks || []).filter(chunk => chunk.status === 'REJECTED').length;
        const automatic=autoReviewSummary(payload.auto_review);
        const automaticDone=automatic && automatic.status==='COMPLETED';
        const mappingGaps=(automatic?.issues?.length || 0)+(automatic?.context_mapping?.issues?.length || 0);
        const needsManual=automatic && (automatic.counts.manual_groups+automatic.counts.manual_assertions+automatic.counts.blocked_assertions+automatic.counts.incomplete+mappingGaps)>0;
        $('construction-next').hidden = false;
        $('construction-next-note').textContent = automatic && hasCandidates
            ? `${autoReviewStageLabel(automatic)}。${mappingGaps?'请查看剩余映射缺口；可补全有明确依据的上下文字段，其他疑问交给人工。':automaticDone && !needsManual?'本次没有待人工判断的记录，可以查看已确认内容并检查发布预览。':'请处理剩余疑难项；未完成的预审可单独重试。'}${rejectedChunks?`另有 ${rejectedChunks} 个片段抽取未通过校验。`:''}`
            : hasCandidates
            ? `已生成可审核记录。下一步先确认实体身份，再审核属性与关系。${rejectedChunks ? `另有 ${rejectedChunks} 个片段抽取未通过校验，请查看下方构建明细。` : ''}`
            : rejectedChunks
              ? '原文已入库，但抽取未通过本体或证据校验，未生成可审核记录。请查看下方构建明细；本次没有新增图谱知识。'
              : payload.extraction_mode === 'SOURCE_ONLY'
                ? '本次选择了“仅保留来源”，未执行实例抽取，因此没有复核候选。要构建知识，请在资料处理方式中选择“按本体抽取”后提交。'
                : '原文已入库，未抽取到可审核实体或事实。本次没有新增图谱知识，请查看下方构建明细。';
        $('construction-next-button').hidden = !hasCandidates;
        $('construction-next-button').textContent = mappingGaps?'下一步：查看待补字段与人工待办 →':automaticDone && !needsManual?'下一步：查看已确认内容 →':automatic?'下一步：处理人工待办 →':'下一步：确认实体与审核事实 →';
        $('construction-next-button').onclick = () => {
          if(automaticDone && !needsManual){showConstructionFlow('business','step-publication');loadPublicationCandidates();}
          else {showConstructionFlow('business','step-review');loadReviews({refreshResolutions:true});}
        };
        return {hasCandidates,automatic};
      }

      function constructionMappingSummary(value) {
        if (!value) return null;
        if (['xml-mapping-extraction:v1', 'markdown-reviewed-mapping-extraction:v1'].includes(value.version)) {
          const unresolved = Array.isArray(value.unresolved_references) ? value.unresolved_references : [];
          const deferred = Array.isArray(value.deferred_properties) ? value.deferred_properties : [];
          return {version:value.version, mapping_checksum:value.mapping_checksum,
            record_count:value.record_count, relationship_count:value.relationship_count,
            property_count:value.property_count, deferred_property_count:value.deferred_property_count || 0,
            unresolved_relationship_count:value.unresolved_relationship_count,
            unresolved_reference_count:unresolved.length,
            unresolved_references:unresolved.slice(0,100).map(item=>({reference:item.reference,
              type:item.type,collection:item.collection,record_count:item.record_count,reason:item.reason,
              uses_count:Array.isArray(item.uses)?item.uses.length:0,
              uses:(item.uses || []).slice(0,100).map(use=>({section_id:use.section_id,side:use.side}))})),
            deferred_properties:deferred.slice(0,100).map(item=>({source_identity:item.source_identity,
              property:item.property,reason:item.reason,mapping_rule_index:item.mapping_rule_index})),
            semantic_context:value.semantic_context || {}, complete:value.complete,
            coverage:Array.isArray(value.coverage)?value.coverage.slice(0,100):value.coverage || {},
            coverage_entry_count:Array.isArray(value.coverage)?value.coverage.length:null,
            details_truncated:unresolved.length>100 || deferred.length>100 || Array.isArray(value.coverage)&&value.coverage.length>100,
            model_calls:value.model_calls ?? value.provider_calls};
        }
        if (!Array.isArray(value.collections)) return null;
        const properties = values => (Array.isArray(values) ? values : []).map(p => ({field:p.field,property:p.property}));
        return {mapping_checksum:value.mapping_checksum,record_count:value.record_count,
          collections:value.collections.map(rule=>({collection:rule.collection,id_field:rule.id_field,
            entity_types:rule.entity_types,record_count:rule.record_count,properties:properties(rule.properties),
            relations:(rule.relations || []).map(r=>({field:r.field,target_collection:r.target_collection,
              target_field:r.target_field,type:r.type,direction:r.direction,properties:properties(r.properties)})),
            retained_fields:rule.retained_fields || []}))};
      }

      function constructionMappingMarkup(mapping) {
        if (Array.isArray(mapping.collections)) {
          return `<details class="construction-result-details"><summary>查看字段映射与未入图字段</summary><p class="field-note">映射由模型提出，程序校验后按原文执行。仅保留原文字段不会自动进入图谱。</p>${mapping.collections.map(rule=>`<article class="exact-evidence"><strong>${escapeHtml(rule.collection || '/')} · ${escapeHtml(rule.record_count)} 条记录</strong><p>类型：${escapeHtml((rule.entity_types || []).join('、'))} · 标识：${escapeHtml(rule.id_field)}</p><p>属性：${rule.properties.map(p=>`${escapeHtml(p.field)} → ${escapeHtml(p.property)}`).join('； ') || '无'}</p><p>关系：${rule.relations.map(r=>`${escapeHtml(r.field)} → ${escapeHtml(r.type)}（${r.direction==='in'?'引用对象 → 本记录':'本记录 → 引用对象'}；${escapeHtml(r.target_collection || '/')} ${escapeHtml(r.target_field)}）`).join('； ') || '无'}</p><p>仅保留原文：${escapeHtml(rule.retained_fields.join('； ') || '无')}</p></article>`).join('')}</details>`;
        }
        const simulated = mapping.semantic_context.content_layer === 'simulated_knowledge';
        const boundary = simulated
          ? '模拟知识文档：用于知识结构与流程验证，不代表已发生故障、实测结果或经过现场验证的结论。'
          : mapping.semantic_context.content_layer === 'engineering_configuration'
            ? '工程组态资料：不代表实测数据或已发生故障；实际通道及测量语义仍需独立核对。'
            : mapping.semantic_context.runtime_event === false ? '本资料不代表已发生的运行事件或实测结果。' : '';
        const coverage = Array.isArray(mapping.coverage)
          ? `<p>章节处置 ${escapeHtml(mapping.coverage_entry_count)} 项（包含前言）；各章已明确用于实体、关系、上下文或保留原文。</p><details><summary>查看章节处置</summary>${mapping.coverage.map(item=>`<p>${escapeHtml(item.section_id || '文档前言')} · ${escapeHtml(({entity:'实体定义',relationship:'关系定义',context:'保留上下文',excluded_context:'仅保留原文',unmapped:'尚未处置'})[item.action] || item.action)}</p>`).join('')}</details>`
          : `<p>原文字段 ${escapeHtml(mapping.coverage.source_fields ?? '—')} 项 · 未处置 ${escapeHtml(mapping.coverage.unmapped_field_count ?? '—')} 项；处置包括映射入图及明确保留原文。</p>`;
        const gapReason = reason => ({external_definition_not_supplied:'当前资料未提供该引用的独立定义，相关关系未入图。',XML_ENUM_LITERAL_PROOF_REQUIRED:'枚举转换尚缺可持久化的映射证据，该属性暂未入图。'})[reason] || reason || '需要进一步核对。';
        const unresolved = mapping.unresolved_reference_count
          ? `<details class="construction-result-details" data-mapping-unresolved><summary>待补引用或语义绑定 ${escapeHtml(mapping.unresolved_reference_count)} 项${mapping.unresolved_relationship_count != null ? ` · 未入图关系 ${escapeHtml(mapping.unresolved_relationship_count)} 条` : ''}</summary>${mapping.unresolved_references.map(item=>`<article class="exact-evidence"><strong>${escapeHtml(item.reference || `${item.collection || ''} → ${item.type || ''}`)}</strong><p>${escapeHtml(gapReason(item.reason))}</p>${item.record_count != null ? `<p>涉及来源记录 ${escapeHtml(item.record_count)} 条</p>` : `<p>引用用途 ${escapeHtml(item.uses_count)} 处 · ${escapeHtml(item.uses.map(use=>use.section_id).join('、'))}</p>`}</article>`).join('')}</details>` : '';
        const deferred = mapping.deferred_property_count
          ? `<details class="construction-result-details" data-mapping-deferred><summary>暂未入图属性 ${escapeHtml(mapping.deferred_property_count)} 项</summary>${mapping.deferred_properties.map(item=>`<article class="exact-evidence"><strong>${escapeHtml(item.property)} · ${escapeHtml(shortId(item.source_identity))}</strong><p>${escapeHtml(gapReason(item.reason))}</p></article>`).join('')}</details>` : '';
        const gaps = mapping.unresolved_reference_count || mapping.deferred_property_count
          ? `<p data-mapping-gap-counts>待补引用或语义绑定 ${escapeHtml(mapping.unresolved_reference_count)} 项${mapping.unresolved_relationship_count != null ? ` · 未入图关系 ${escapeHtml(mapping.unresolved_relationship_count)} 条` : ''} · 暂未入图属性 ${escapeHtml(mapping.deferred_property_count)} 项。原文与未解决项继续保留。</p>` : '';
        return `<section class="construction-result-details" data-document-mapping><p><strong>按已提供的映射配置执行 · 本次映射的模型调用：${escapeHtml(mapping.model_calls)}</strong></p>${boundary?`<p class="exact-evidence" data-mapping-semantic-boundary>${escapeHtml(boundary)}</p>`:''}${gaps}<details><summary>查看映射覆盖与未入图内容</summary>${coverage}<p>已解析关系 ${escapeHtml(mapping.relationship_count)} 条${mapping.property_count != null ? ` · 已识别属性 ${escapeHtml(mapping.property_count)} 项` : ''}；是否入图及发布需结合待补项与审核状态。</p>${unresolved}${deferred}${mapping.details_truncated?'<p class="field-note">每类明细最多展示 100 项，总数以上方统计为准；完整处置记录保留在构建审计中。</p>':''}</details></section>`;
      }

      function constructionChunkSummary(item) {
        return {
          chunk_id: item.chunk_id, artifact_id: item.artifact_id,
          status: item.status, findings: item.finding_codes || [],
          mentions: (item.mention_record_ids || []).length,
          assertions: (item.assertion_record_ids || []).length,
          mention_record_ids: item.mention_record_ids || [],
          assertion_record_ids: item.assertion_record_ids || [], replayed: item.replayed,
          ...(item.mapping_summary ? {mapping_summary:constructionMappingSummary(item.mapping_summary)} : {}),
          validation_attempts: (Array.isArray(item.validation_attempts) ? item.validation_attempts : []).map(attempt => ({
            attempt: attempt.attempt, status: attempt.status,
            finding_codes: Array.isArray(attempt.finding_codes) ? attempt.finding_codes : [],
            response_checksum: attempt.response_checksum ?? null,
          })),
        };
      }

      function constructionValidationMarkup(item) {
        const attempts = item.validation_attempts;
        if (item.findings.includes('STRUCTURED_MAPPING_APPLIED')) return `<article class="governance-item"><strong>Chunk ${escapeHtml(shortId(item.chunk_id))} · 程序映射校验通过</strong><p>已核对本体约束与精确原文位置，候选仍需复核发布。</p></article>`;
        if (!attempts.length) return `<article class="governance-item"><strong>Chunk ${escapeHtml(shortId(item.chunk_id))}</strong><p>${item.status === 'SOURCE_ONLY' ? '仅来源入库，未执行 LLM 抽取或抽取校验。' : '该结果未提供逐次校验摘要；请结合任务状态和 findings 查看结果。'}</p></article>`;
        const passed = ['CANDIDATE', 'EMPTY'].includes(item.status);
        const conclusion = passed
          ? `${attempts.length > 1 ? '第二次校验通过' : '首次校验通过'}${item.status === 'EMPTY' ? '，没有可抽取事实。' : '，候选仍需人工审核和明确发布。'}`
          : item.status === 'QUARANTINED' ? '结果已隔离，请人工核对 findings；尚未发布。'
          : '校验仍未通过，本次结果不能发布。';
        const statusLabels = {CANDIDATE: '校验通过', QUARANTINED: '隔离待核对', REJECTED: '校验未通过', PROVIDER_ERROR: '模型服务错误'};
        const rows = attempts.map(attempt => `<div class="exact-evidence"><strong>第 ${escapeHtml(attempt.attempt)} 次校验：${escapeHtml(statusLabels[attempt.status] || attempt.status)}</strong><br>findings：${escapeHtml(attempt.finding_codes.join(' · ') || '无')}${attempt.response_checksum ? `<br>响应摘要 SHA256：${escapeHtml(attempt.response_checksum)}` : ''}</div>`).join('');
        return `<article class="governance-item"><strong>Chunk ${escapeHtml(shortId(item.chunk_id))} · ${escapeHtml(conclusion)}</strong>${rows}</article>`;
      }

      function showConstructionResult(payload) {
        state.constructionReceipt=payload;
        const autoReview=rememberAutoReview(payload.auto_review);
        const keys = ['job_id', 'extraction_mode', 'chunking', 'document_id', 'version_id', 'snapshot_id', 'tbox_id',
          'status', 'expected_chunks', 'completed_chunks', 'created_at', 'updated_at', 'completed_at',
          'failed_chunk_id', 'last_finding_codes'];
        const summary = Object.fromEntries(keys.filter(key => payload[key] !== undefined).map(key => [key, payload[key]]));
        summary.chunks = (Array.isArray(payload.chunks) ? payload.chunks : []).map(constructionChunkSummary);
        if(autoReview)summary.auto_review=autoReview;
        const statusLabel = ({RUNNING:'处理中', RETRY_WAIT:'已中断，等待重试', FAILED:'已失败', COMPLETED:'已完成'})[payload.status]
          || payload.status || '结果已返回';
        const expected = Number.isInteger(payload.expected_chunks) ? payload.expected_chunks : summary.chunks.length;
        const completed = Number.isInteger(payload.completed_chunks) ? payload.completed_chunks : summary.chunks.length;
        const counts = new Map();
        for(const chunk of summary.chunks) counts.set(chunk.status,(counts.get(chunk.status) || 0)+1);
        const categories = [['CANDIDATE',autoReview?'抽取通过':'待复核'],['REJECTED','校验未通过'],['EMPTY','无抽取结果'],['SOURCE_ONLY','仅保存来源'],['QUARANTINED','隔离待核对']];
        const countMarkup = categories.map(([status,label]) => `<span data-construction-count="${status}">${label} <strong>${counts.get(status) || 0}</strong></span>`).join('');
        const known = new Set(categories.map(([status])=>status));
        const other = summary.chunks.filter(chunk=>!known.has(chunk.status)).length;
        const mapping = summary.chunks.find(chunk=>chunk.mapping_summary)?.mapping_summary;
        const mappingMarkup = mapping ? constructionMappingMarkup(mapping) : "";
        output(elements.constructionOutput, '');
        elements.constructionOutput.innerHTML = `<div class="construction-result-overview"><strong>构建任务：${escapeHtml(statusLabel)}</strong><p>已处理 ${completed} / ${expected} 个片段</p><div class="construction-result-counts">${countMarkup}${other ? `<span>其他结果 <strong>${other}</strong></span>` : ''}</div><p class="field-note">${autoReview?'抽取结果已进入自动预审；审核结果见下方。':'候选需经人工复核，再明确发布为正式知识。'}</p></div>${autoReviewMarkup(autoReview)}<details class="construction-result-details"><summary>查看结构化摘要与任务标识</summary><pre class="construction-structured-summary" data-construction-summary tabindex="0" aria-label="构建任务结构化摘要">${escapeHtml(JSON.stringify(summary,null,2))}</pre></details>`;
        if (mapping) elements.constructionOutput.innerHTML = elements.constructionOutput.innerHTML.replace('<p>已处理', `<p>结构化映射构建 · ${escapeHtml(mapping.record_count)} 条来源记录</p><p>已处理`) + mappingMarkup;
        elements.constructionOutput.querySelector('.construction-result-overview').insertAdjacentHTML('beforeend',chunkingMarkup(payload.chunking));
        $('construction-validation-summary').innerHTML = `<details class="construction-validation-details"><summary>逐片段校验记录（${summary.chunks.length}）</summary><div class="construction-validation-list">${summary.chunks.map(constructionValidationMarkup).join('') || '<p>当前尚无已保存的片段结果。</p>'}</div></details>`;
        bindAutoReviewActions(elements.constructionOutput);
        if(autoReview)renderConstructionNext(payload);
      }

      function bindDemoAuthority(payload, metadata, contentHash) {
        const kit = demoKit();
        const source = kit?.files.find(item => item.id === 'authoritative_source');
        if (metadata.extraction_mode !== 'SOURCE_ONLY') return false;
        if (!source || metadata.tbox_key !== kit.ontology_key) {
          refreshABoxPreparation('权威来源已入库，但当前本体不是泵站示例本体，未自动填写示例实例。请按当前本体和来源证据填写专家实例 JSON；使用示例请先在本体构建区发布 pump-maintenance-demo 本体。');
          return false;
        }
        // Source addresses and titles are user metadata. Only exact file bytes
        // authorize the committed demo's evidence spans; IDs come from this upload.
        if (contentHash !== source.sha256) {
          const message = '权威来源已入库，但文件内容与泵站示例不一致，未自动填写实例。请按实际来源证据填写专家实例 JSON；使用示例请上传未修改的 authoritative_source.txt。';
          refreshABoxPreparation(message);
          output($('demo-note'), message);
          return false;
        }
        if (payload.extraction_mode !== 'SOURCE_ONLY' || payload.chunks?.length !== 1 || payload.chunks[0].status !== 'SOURCE_ONLY' || payload.chunks[0].mention_record_ids?.length || payload.chunks[0].assertion_record_ids?.length) throw new Error('来源入库结果与示例合同不一致，未生成专家实例。');
        const ids = {ontology_version_id: payload.tbox_id, document_id: payload.document_id, version_id: payload.version_id, chunk_id: payload.chunks[0].chunk_id};
        if (Object.values(ids).some(value => typeof value !== 'string' || !value.trim())) throw new Error('来源返回缺少稳定 ID，未生成专家实例。');
        const replacements = Object.fromEntries(Object.entries(kit.placeholders).map(([key, placeholder]) => [placeholder, ids[key]]));
        const bind = value => typeof value === 'string' ? (replacements[value] ?? value)
          : Array.isArray(value) ? value.map(bind)
          : value && typeof value === 'object' ? Object.fromEntries(Object.entries(value).map(([key, entry]) => [key, bind(entry)])) : value;
        state.expertRevisionIds = [];
        state.expertImportMayReplace = false;
        $('abox-publication-panel').hidden = true;
        elements.aboxEditor.value = JSON.stringify(bind(kit.authoritative_import_template), null, 2);
        state.demoSourceBinding = ids;
        refreshABoxPreparation('已按刚入库的真实文档 / 版本 / Chunk ID 填写专家实例。请查看 JSON，点击“校验并导入权威层”，随后在同一区域点击“发布专家基准”。');
        output($('demo-note'), '权威来源已入库，未调用抽取模型。配套实例已填入专家实例编辑器，尚未导入或发布。');
        return true;
      }

      function detectedMime(file) {
        const extension = String(file.name || '').split('.').pop().toLowerCase();
        const inferred = mimeByExtension[extension];
        const selected = String(file.type || '').split(';')[0].trim().toLowerCase();
        // Browsers report XML as either MIME alias, and may omit MIME metadata.
        const xmlAlias = extension === 'xml' && ['application/xml','text/xml'].includes(selected);
        const unspecified = !selected || selected === 'application/octet-stream';
        if (!inferred || (!unspecified && selected !== inferred && !xmlAlias)) {
          throw new Error('仅支持匹配扩展名的 UTF-8 txt/md/csv/json/xml；请检查文件格式。');
        }
        return inferred;
      }

      function bytesToBase64(bytes) {
        let binary = '';
        for (let offset = 0; offset < bytes.length; offset += 0x8000) {
          binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
        }
        return btoa(binary);
      }

      async function constructionFingerprint(bytes, metadata) {
        if (!globalThis.crypto?.subtle) throw new Error('浏览器不支持安全的构建重试指纹');
        const digest = await globalThis.crypto.subtle.digest('SHA-256', bytes);
        const contentHash = [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, '0')).join('');
        return JSON.stringify({content_sha256: contentHash, ...metadata, access_groups: [...metadata.access_groups].sort()});
      }

      async function nextConstructionOperation(fingerprint, identityEpoch) {
        let operation = state.constructionOperation;
        if (!operation) {
          try { operation = JSON.parse(sessionStorage.getItem('graphrag-construction-operation') || 'null'); } catch (_) {}
        }
        if (operation?.fingerprint === fingerprint && operation.operationKey) {
          // A lost error response can leave a terminal operation in sessionStorage.
          // Preserve unknown/running operations for idempotent replay; only a
          // server-confirmed terminal failure starts a fresh bounded attempt.
          const jobs = await apiRequest('/v1/knowledge/construction-jobs?limit=25', {feedback:false});
          if (identityEpoch !== state.identityEpoch) return null;
          const previous = jobs.items?.find(item => item.operation_key === operation.operationKey);
          if (previous?.status === 'FAILED') operation = null;
        }
        if (!operation || operation.fingerprint !== fingerprint || !operation.operationKey) {
          const nonce = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
          operation = {fingerprint, operationKey: `playground-${nonce}`};
          try { sessionStorage.setItem('graphrag-construction-operation', JSON.stringify(operation)); } catch (_) {}
        }
        state.constructionOperation = operation;
        return operation.operationKey;
      }

      function completeConstructionOperation() {
        state.constructionOperation = null;
        try { sessionStorage.removeItem('graphrag-construction-operation'); } catch (_) {}
      }

      function renderConstructionJobs() {
        if (!state.constructionJobs.length) {
          elements.constructionJobList.innerHTML = '<div class="output-box">当前身份没有可见的构建任务。</div>';
          return;
        }
        elements.constructionJobList.innerHTML = state.constructionJobs.map((item, index) => `<article class="governance-item"><div class="governance-item-head"><div><strong>${escapeHtml(({RUNNING:'处理中', RETRY_WAIT:'已中断，等待重试', FAILED:'已失败', COMPLETED:'已完成'})[item.status] || item.status)} · ${escapeHtml(shortId(item.job_id))}</strong><p>${item.extraction_mode === 'SOURCE_ONLY' ? '仅保存来源 · 未抽取候选' : '实例构建'} · ${escapeHtml(item.completed_chunks)} / ${escapeHtml(item.expected_chunks)} Chunks · document ${escapeHtml(shortId(item.document_id))}<br>updated ${escapeHtml(item.updated_at)}</p></div><span class="trust-badge ${item.status === 'COMPLETED' ? 'authoritative' : ''}">${escapeHtml(item.status)}</span></div>${constructionFailureMarkup(item)}<div class="workbench-actions">${item.status === 'RETRY_WAIT' && item.operation_key ? `<button class="button primary" type="button" data-construction-job-resume="${index}">继续此任务</button>` : ''}<button class="button" type="button" data-construction-job-detail="${index}">查看任务明细</button></div></article>`).join('');
        elements.constructionJobList.querySelectorAll('[data-construction-job-resume]').forEach(button => {
          button.addEventListener('click', foregroundAction('', () => resumeConstructionJob(Number(button.dataset.constructionJobResume))));
        });
        elements.constructionJobList.querySelectorAll('[data-construction-job-detail]').forEach(button => {
          button.addEventListener('click', foregroundAction('', () => loadConstructionJob(Number(button.dataset.constructionJobDetail), button)));
        });
      }

      function constructionFailureMarkup(item) {
        const codes = item.last_finding_codes || [];
        if (!codes.length && !item.failed_chunk_id) return '';
        const reason = codes.includes('EMBEDDING_CONNECTION_ERROR') ? '向量化服务连接失败，尚未进入本次 LLM 抽取。'
          : codes.includes('EMBEDDING_TIMEOUT') ? '向量化服务连接或响应超时，尚未进入本次 LLM 抽取。'
          : codes.includes('SOURCE_INGESTION_FAILED') ? '来源入库阶段失败，尚未进入本次 LLM 抽取。'
          : codes.includes('MODEL_CALL_TIMEOUT') ? '模型抽取请求超时，已完成的片段保留。'
          : codes.includes('MODEL_CONNECTION_ERROR') ? '模型服务连接中断，已完成的片段保留。'
          : codes.includes('MODEL_ACCOUNT_ARREARS') ? '模型服务商返回账户欠费或账户状态异常。请核对当前密钥所属账户；此错误不代表所选模型的免费额度已用完。已完成的片段保留。'
          : codes.includes('MODEL_QUOTA_EXHAUSTED') ? '模型服务报告可用额度不足，已完成的片段保留。请恢复可用额度，或由管理员配置可用模型后重新构建。'
          : codes.includes('MODEL_ACCESS_DENIED') ? '当前密钥或业务空间没有所选模型的调用权限。请核对模型权限与服务配置，已完成的片段保留。'
          : codes.includes('MODEL_CALL_FAILED') ? '模型调用或响应读取失败，已完成的片段保留。'
          : codes.some(code=>code.startsWith('MAPPING_')) ? '字段映射未通过校验，尚未生成本次候选。请核对字段、设备标识和引用关系。'
          : '构建已中断，请查看任务明细中的原因。';
        const nextStep = item.status === 'FAILED'
          ? '本次任务已结束，旧记录保留。依赖恢复后，请重新提交同一文件以创建新任务；刷新列表不会自动重试。'
          : item.status === 'RETRY_WAIT' && item.operation_key
            ? '恢复后选择原文件并点击“继续此任务”；刷新列表只查看进度。'
            : '请查看任务明细；刷新列表只查看进度。';
        return `<div class="exact-evidence">${escapeHtml(reason)} ${nextStep}<br>${escapeHtml(codes.join(' · '))}</div>`;
      }

      async function loadConstructionJobs() {
        const finish=state.reviewBusy || state.publicationBusy ? () => {} : beginButtonFeedback($('construction-jobs-refresh-button'));
        try {
        const identityEpoch = state.identityEpoch;
        const autoReviewEpoch=state.autoReviewEpoch || 0;
        try {
          const payload = await apiRequest('/v1/knowledge/construction-jobs?limit=25');
          if (identityEpoch !== state.identityEpoch || autoReviewEpoch!==(state.autoReviewEpoch || 0)) return;
        state.constructionJobs = Array.isArray(payload.items) ? payload.items : [];
          renderConstructionJobs();
          if (!state.reviewLoading && !state.reviewBusy) renderReviews();
          const latest=state.constructionJobs.find(item=>item.status==='COMPLETED');
          if(latest && !state.constructionBusy && !state.autoReviewBusy) {
            try {
              await refreshConstructionReceipt(latest.job_id,{renderReceipt:state.constructionReceipt?.job_id===latest.job_id});
            } catch(_) { /* Job history remains available if its optional pre-review summary cannot be read. */ }
          }
        } catch (error) {
          if (identityEpoch !== state.identityEpoch) return;
          elements.constructionJobList.innerHTML = `<div class="output-box">${escapeHtml(error.message)}</div>`;
        }
        } finally {finish();}
      }

      async function resumeConstructionJob(index) {
        if (state.constructionBusy) { showToast('请等待当前构建结束。'); return; }
        const item = state.constructionJobs[index];
        if (!item?.operation_key) { showToast('任务列表已变化，请刷新。'); return; }
        const identityEpoch = state.identityEpoch;
        try {
          // Recheck authorization and current status; never derive a new
          // operation from browser storage when resuming an explicit job.
          const current = await constructionJobDetail(item.job_id);
          if (identityEpoch !== state.identityEpoch) return;
          if (current.job_id !== item.job_id || current.operation_key !== item.operation_key || current.status !== 'RETRY_WAIT') {
            showToast('此任务当前不可续跑，请刷新任务列表。');
            await loadConstructionJobs();
            return;
          }
          if (!$('document-file').files?.[0]) {
            output(elements.constructionOutput, '请在上方选择此任务的原文件，保持原来的本体、来源和切片设置，然后点击下方“继续此任务”。已完成的片段保留。');
            $('document-file').scrollIntoView?.({block:'center', behavior:'smooth'});
            return;
          }
          await constructKnowledge(null, {resumeJob: current});
        } catch (error) {
          if (identityEpoch === state.identityEpoch) output(elements.constructionOutput, error.message);
        }
      }

      async function loadConstructionJob(index, button) {
        const item = state.constructionJobs[index];
        if (!item) { showToast('构建任务列表已变化，请刷新'); return; }
        const identityEpoch = state.identityEpoch;
        const autoReviewEpoch=state.autoReviewEpoch || 0;
        const draftEpoch = state.uploadDraftEpoch;
        button.disabled = true;
        try {
          const payload = await constructionJobDetail(item.job_id);
          if (identityEpoch !== state.identityEpoch || draftEpoch !== state.uploadDraftEpoch || autoReviewEpoch!==(state.autoReviewEpoch || 0)) return;
          showConstructionResult(payload);
          if (payload.status === 'FAILED' || payload.status === 'RETRY_WAIT') {
            $('construction-validation-summary').innerHTML = constructionFailureMarkup(payload) + $('construction-validation-summary').innerHTML;
          }
        } catch (error) {
          if (identityEpoch === state.identityEpoch) showToast(error.message);
        } finally {
          button.disabled = false;
        }
      }

      function showUploadPreflight(report, fingerprint) {
        const matches = [...report.exact_matches, ...report.similar_matches];
        const exact = report.exact_matches.length > 0;
        const box = elements.constructionOutput;
        box.hidden = false;
        box.classList.add('upload-check');
        box.innerHTML = `<strong>${exact ? '这份资料已经存在，已暂停重复构建' : matches.length ? '发现高度相似的资料，请先核对变化' : '相似资料检查未覆盖全部范围'}</strong>
          <p>${exact ? '文件名或来源地址不同，也会按内容识别。保留已有资料即可，无需再次抽取和审核。' : '相似不代表相同。设备编号、数值或事实发生变化时，可以按当前来源信息继续构建。'}</p>
          ${chunkingMarkup(report.chunking,{preview:true})}
          ${matches.map((item,index) => `<article class="upload-match"><div><strong>${escapeHtml(item.title)}</strong><span class="trust-badge">${item.match_kind === 'EXACT' ? '内容相同' : `文本相似度 ${(item.similarity * 100).toFixed(1)}%`}</span></div><p>${escapeHtml(item.canonical_uri)}</p>
            ${item.difference ? `<div class="upload-difference"><div><span>已有资料 · 首处差异附近</span><pre>${escapeHtml(item.difference.before)}</pre></div><div><span>本次上传 · 首处差异附近</span><pre>${escapeHtml(item.difference.after)}</pre></div></div>` : ''}
            ${onSource ? `<button class="text-button" type="button" data-upload-existing="${index}">查看已有原文</button>` : ''}</article>`).join('')}
          ${report.truncated ? '<p class="field-note">本次相似检查受范围限制，未覆盖所有可见资料；没有列出不代表不存在相似内容。最多比较 50 个长度接近的版本，每份不超过 20 万字，完整内容仍按校验和查重。</p>' : ''}
          <div class="workbench-actions"><button class="button primary" type="button" data-upload-skip>${exact ? '不重复构建' : '暂不构建'}</button>${exact ? '' : '<button class="button" type="button" data-upload-continue>已核对，继续构建</button>'}</div>
          ${exact ? '<details><summary>仍需使用当前设置重新构建</summary><p>仅在需要独立保留另一来源，或更改抽取方式、本体等设置时继续；这可能新增待审核记录。</p><button class="button" type="button" data-upload-continue>确认重新构建</button></details>' : ''}`;
        box.querySelector('[data-upload-skip]').onclick = () => {
          completeConstructionOperation();
          output(box, '已取消本次构建。已有资料和审核结果继续保留，未调用抽取模型。');
        };
        box.querySelector('[data-upload-continue]').onclick = () => constructKnowledge({fingerprint, token: report.review_token});
        box.querySelectorAll('[data-upload-existing]').forEach(button => {
          button.onclick = () => {
            const item = matches[Number(button.dataset.uploadExisting)];
            onSource({document_id:item.document_id, version_id:item.version_id, document_title:item.title, ordinal:0});
          };
        });
        box.scrollIntoView?.({block:'nearest',behavior:'smooth'});
      }

      async function constructKnowledge(acknowledgment = null, {previewOnly=false, resumeJob=null}={}) {
        if (state.constructionBusy) return;
        const file = $('document-file').files?.[0];
        if (!file) { output(elements.constructionOutput, '请选择一个文档。'); return; }
        if (!file.size || file.size > MAX_UPLOAD_BYTES) { output(elements.constructionOutput, '文件必须在 1 byte 到 5 MiB 之间。'); return; }
        const identityEpoch = state.identityEpoch;
        const knowledgeScope = $('document-knowledge-scope').value;
        if (!['BUSINESS', 'AUTHORITATIVE'].includes(knowledgeScope)) { output(elements.constructionOutput, '请选择有效的资料类型。'); return; }
        state.uploadKnowledgeScope = knowledgeScope;
        state.constructionBusy = true;
        uploadMapping.sync();
        clearConstructionResult();
        const draftControls = [...$('upload-card').querySelectorAll('input, select')];
        const disabledControls = draftControls.map(control => control.disabled);
        $('document-knowledge-scope').disabled = true;
        $('construction-next').hidden = true;
        $('construction-next-button').hidden = false;
        $('construction-validation-summary').innerHTML = '';
        $('construct-button').disabled = true;
        $('preview-chunks-button').disabled = true;
        let finishFeedback = beginOperationFeedback('正在检查上传资料…', {button:$(previewOnly?'preview-chunks-button':'construct-button')});

        let uploadRequest = null, uploadFingerprint = null, stopProgress = () => {};
        try {
          const mime = detectedMime(file);
          const title = $('document-title').value.trim();
          const canonicalUri = $('document-uri').value.trim();
          const sourceName = $('document-source').value.trim();
          const tboxKey = $('document-tbox').value.trim();
          const language = $('document-language').value.trim();
          const extractionMode = $('document-extraction-mode').value;
          const mappingProfile=uploadMapping.request(mime);
          const chunking = selectedChunking();
          if (!['LLM', 'SOURCE_ONLY'].includes(extractionMode)) throw new Error('构建方式无效');
          const accessGroups = selectedDocumentAccessGroups();
          if (!title || !canonicalUri || !sourceName || !tboxKey || !language) throw new Error('请完整填写文档元数据');
          if (!accessGroups.length) throw new Error('至少选择一个新文档访问组');
          draftControls.forEach(control => { control.disabled = true; });
          if (!await requirePublishedConstructionOntology(tboxKey, identityEpoch)) return;

          const bytes = new Uint8Array(await file.arrayBuffer());
          if (identityEpoch !== state.identityEpoch) return;
          const industrialContext = uploadContext();
          const metadata = {
            industrial_context: industrialContext,
            persona_id: currentPersona()?.id,
            canonical_uri: canonicalUri,
            title,
            source_name: sourceName,
            mime_type: mime,
            language,
            tbox_key: tboxKey,
            access_groups: accessGroups,
            extraction_mode: extractionMode,
            knowledge_scope: knowledgeScope,
            chunking,
            ...(mappingProfile?{mapping_profile:mappingProfile}:{}),
          };
          const fingerprint = await constructionFingerprint(bytes, metadata);
          if (identityEpoch !== state.identityEpoch) return;
          const operationKey = resumeJob?.operation_key || await nextConstructionOperation(fingerprint, identityEpoch);
          if (identityEpoch !== state.identityEpoch) return;
          uploadFingerprint = fingerprint;
          uploadRequest = {
            operation_key: operationKey, canonical_uri: canonicalUri, title,
            source_name: sourceName, mime_type: mime, language, tbox_key: tboxKey,
            extraction_mode: extractionMode, knowledge_scope: metadata.knowledge_scope,
            industrial_context: industrialContext, access_groups: accessGroups,
            chunking,
            ...(mappingProfile?{mapping_profile:mappingProfile}:{}),
            max_attempts: 1, content_base64: bytesToBase64(bytes),
          };
          const preflight = await apiRequest('/v1/knowledge:preflight', {
            method:'POST', feedback:false, headers:{'Content-Type':'application/json'},
            body:JSON.stringify(uploadRequest),
          });
          if (identityEpoch !== state.identityEpoch) return;
          if (previewOnly) {
            output(elements.constructionOutput,'');
            const duplicates = preflight.exact_matches.length ? '发现内容相同的已有资料；正式构建前仍需核对。'
              : preflight.similar_matches.length ? '发现相似资料；正式构建前仍需核对。' : '';
            elements.constructionOutput.innerHTML = `${chunkingMarkup(preflight.chunking,{preview:true}) || '<p>当前服务未返回切片统计。</p>'}${mappingPreflightMarkup(preflight.mapping_preflight)}<p class="field-note">仅预览切片与映射，尚未保存来源或调用抽取模型。${duplicates}${preflight.truncated ? '相似检查未覆盖全部可见资料。' : ''}</p>`;
            return;
          }
          if(mappingProfile && !preflight.mapping_preflight?.valid){output(elements.constructionOutput,'');elements.constructionOutput.innerHTML=mappingPreflightMarkup(preflight.mapping_preflight)||'<p>服务未返回映射预检结果，已停止构建。</p>';return;}
          if(extractionMode!=='SOURCE_ONLY')uploadMapping.checkPreflight(preflight.mapping_preflight);
          const needsReview = preflight.exact_matches.length || preflight.similar_matches.length || preflight.truncated;
          if (resumeJob && resumeJob.completed_chunks > 0 && !preflight.exact_matches.some(item => item.document_id === resumeJob.document_id && item.version_id === resumeJob.version_id)) {
            throw new Error('所选文件与此任务的资料版本不一致，已停止续跑。请选择原文件；已完成片段保留，本次未新建任务。');
          }
          // Explicit resume already selects the existing document and job.
          // The backend still verifies its complete immutable fingerprint.
          if (!resumeJob && needsReview && (acknowledgment?.fingerprint !== fingerprint || acknowledgment?.token !== preflight.review_token)) {
            showUploadPreflight(preflight, fingerprint);
            return;
          }
          if (needsReview) uploadRequest.preflight_token = preflight.review_token;
          finishFeedback();
          const progress = extractionMode === 'SOURCE_ONLY' ? '正在入库与向量化…'
            : mappingProfile?'正在按已审校映射构建并校验原文证据，不调用提取模型…'
            : mime === 'application/json' ? '正在入库与识别 JSON 结构；记录型资料将生成字段映射后由程序构建…'
            : '正在构建，解析与抽取可能需要一些时间…';
          finishFeedback = beginOperationFeedback(progress, {button:$('construct-button')});
          output(elements.constructionOutput,'');
          elements.constructionOutput.innerHTML = chunkingMarkup(preflight.chunking,{preview:true});
          stopProgress = watchConstructionProgress(operationKey, identityEpoch, progress);

          const payload = await apiRequest('/v1/knowledge:construct', {
            method:'POST', feedback:false, headers:{'Content-Type':'application/json'},
            body:JSON.stringify(uploadRequest),
          });
          if (identityEpoch !== state.identityEpoch) return;
          stopProgress();
          completeConstructionOperation();
          state.uploadDraftCompleted = true;
          state.knowledgeBrowser?.reset();
          showConstructionResult(payload);
          state.lastConstructionScope = metadata.knowledge_scope;
          state.lastConstructionMode = extractionMode;
          const {hasCandidates,automatic}=renderConstructionNext(payload);
          showToast(extractionMode === 'SOURCE_ONLY' ? `来源入库完成：${payload.chunks.length} Chunks，未调用抽取模型` : automatic?autoReviewStageLabel(automatic):hasCandidates ? '已生成可审核记录，请核对抽取结果' : '原文已入库，本次未生成可审核记录');
          await Promise.allSettled([loadConstructionJobs(), loadReviews(), loadActiveDocuments()]);
          if(automatic && identityEpoch===state.identityEpoch)await loadPublicationCandidates();
        } catch (error) {
          if (identityEpoch === state.identityEpoch) {
            if (resumeJob && ['conflict', 'construction_chunking_conflict', 'upload_review_required'].includes(error.code)) {
              output(elements.constructionOutput, '续跑条件与原任务不一致，或资料状态已变化。请核对原文件、本体、来源及切片设置；模型配置变化也会阻止复用。已完成片段保留，本次未新建任务。');
              await loadConstructionJobs();
              return;
            }
            if (error.code === 'upload_review_required' && uploadRequest) {
              try {
                const checked = await apiRequest('/v1/knowledge:preflight', {method:'POST', feedback:false, headers:{'Content-Type':'application/json'}, body:JSON.stringify(uploadRequest)});
                if (identityEpoch === state.identityEpoch) showUploadPreflight(checked, uploadFingerprint);
              } catch (checkError) { if (identityEpoch === state.identityEpoch) output(elements.constructionOutput, checkError.message); }
              return;
            }
            if (error.code === 'construction_chunking_conflict') {
              completeConstructionOperation();
              output(elements.constructionOutput, '该资料版本已有不同的切片结果，不能用新设置覆盖已有片段。请保留现有版本；更新资料内容后，可使用新设置构建新版本。');
            } else if (error.code === 'construction_mapping_invalid') {
              completeConstructionOperation();
              output(elements.constructionOutput, error.message);
            } else if (error.code === 'construction_ingestion_failed') {
              completeConstructionOperation();
              output(elements.constructionOutput, '来源入库或向量化失败，本次任务已结束。请查看下方任务原因；依赖恢复后，再点击上传会创建新任务，保留旧任务记录。');
            } else output(elements.constructionOutput, error.message);
            await loadConstructionJobs();
          }
        } finally {
          stopProgress();
          finishFeedback();
          if (identityEpoch === state.identityEpoch) {
            state.constructionBusy = false;
            draftControls.forEach((control, index) => { control.disabled = disabledControls[index]; });
            $('construct-button').disabled = false;
            $('preview-chunks-button').disabled = false;
            updateConstructionMode();
          }
        }
      }

      function watchConstructionProgress(operationKey, identityEpoch, initialMessage) {
        let stopped = false, timer = null;
        const display = $('construction-progress');
        display.hidden = false;
        display.textContent = initialMessage;
        const poll = async () => {
          if (stopped || identityEpoch !== state.identityEpoch) return;
          try {
            const payload = await apiRequest('/v1/knowledge/construction-jobs?limit=25', {feedback:false});
            if (stopped || identityEpoch !== state.identityEpoch) return;
            const job = payload.items?.find(item => item.operation_key === operationKey);
            if (job) {
              const label = {RUNNING:'正在处理', COMPLETED:'片段处理完成，正在汇总', RETRY_WAIT:'任务已中断，等待重试', FAILED:'任务处理失败'}[job.status] || job.status;
              display.textContent = `${label}：${job.completed_chunks} / ${job.expected_chunks} 个片段。每个片段均需通过本体与证据校验。`;
              if(job.status==='COMPLETED') {
                const detail=await apiRequest(`/v1/knowledge/construction-jobs/${encodeURIComponent(job.job_id)}`,{feedback:false});
                if(stopped || identityEpoch!==state.identityEpoch)return;
                const automatic=rememberAutoReview(detail.auto_review);
                if(automatic)display.textContent=`片段处理 ${job.completed_chunks} / ${job.expected_chunks}；${autoReviewStageLabel(automatic)}。`;
              }
            }
          } catch (_) {
            if (!stopped && identityEpoch === state.identityEpoch) display.textContent = '进度暂时无法刷新，正在等待本次构建结果。';
          } finally {
            if (!stopped && identityEpoch === state.identityEpoch) timer = setTimeout(poll, 3000);
          }
        };
        timer = setTimeout(poll, 1000);
        return () => {
          stopped = true;
          clearTimeout(timer);
          if (identityEpoch === state.identityEpoch) { display.hidden = true; display.textContent = ''; }
        };
      }

      function reviewEdit(item) {
        // Response identities also contain a server-owned entity_id. Only
        // KnowledgeEntityInput fields belong in a strict review edit request.
        const entityInput = entity => ({
          entity_type: entity.entity_type,
          canonical_key: entity.canonical_key,
          canonical_name: entity.canonical_name,
          aliases: [...(entity.aliases || [])],
        });
        if (item.record_kind === 'ENTITY_MENTION') {
          return {entity: entityInput(item.entity), confidence: item.confidence};
        }
        const edit = {
          subject: entityInput(item.subject),
          predicate: item.predicate,
          subject_mention_revision_id: item.subject_mention_revision_id,
          confidence: item.confidence,
        };
        if (item.object_entity) {
          edit.object_entity = entityInput(item.object_entity);
          edit.object_mention_revision_id = item.object_mention_revision_id;
          edit.relationship_properties = (item.relationship_properties || []).map(value => {
            const semantics = literalSemantics(value);
            const literal = {raw_literal: semantics.raw_value};
            for (const field of ['raw_unit', 'raw_valid_from', 'raw_valid_to', 'raw_observed_at', 'source_encoding']) {
              if (semantics[field] != null) literal[field] = semantics[field];
            }
            return {name: value.name, literal, evidence: value.evidence, confidence: value.confidence};
          });
          return edit;
        }
        const semantics = literalSemantics(item);
        edit.literal = {raw_literal: semantics.raw_value ?? item.literal_value};
        for (const field of ['raw_unit', 'raw_valid_from', 'raw_valid_to', 'raw_observed_at', 'source_encoding']) {
          if (semantics[field] != null) edit.literal[field] = semantics[field];
        }
        return edit;
      }

      function propertyAssignment(item) {
        const entry=state.propertyAssignments?.get(item.record_id);
        return entry?.revision===item.revision && entry.identityEpoch===state.identityEpoch && entry.reviewEpoch===state.reviewEpoch ? entry : null;
      }
      function assignmentEntityLabel(entity, properties=[]) {
        const identity=properties.map(value=>`${reviewPropertyLabel(value.name)}：${value.value}${value.unit ? ` ${value.unit}` : ''}`).join(' · ');
        return `${entity.canonical_name} · ${identity || entity.canonical_key || shortId(entity.entity_id)}`;
      }
      function propertyAssignmentMarkup(item,index) {
        if(item.record_kind!=='ASSERTION' || item.object_entity) return '';
        const entry=propertyAssignment(item), current=entry?.current;
        const label=assignmentEntityLabel(item.subject,current?.identity_properties);
        const header=`<div class="property-assignment-current"><span>当前归属：<strong>${escapeHtml(label)}</strong></span><button class="button" type="button" data-assignment-toggle="${index}">${entry?.open ? '取消更改' : '更改归属'}</button></div>`;
        if(!entry?.open) return header;
        const choices=new Map([[item.subject.entity_id,{entity:item.subject,identity_properties:current?.identity_properties || [],selectable:true}]]);
        for(const target of entry.items || []) choices.set(target.entity.entity_id,target);
        const selected=choices.get(entry.selected);
        return header+`<div class="property-assignment-editor"><label>搜索实体<input data-assignment-query="${index}" value="${escapeHtml(entry.query || '')}" maxlength="200" placeholder="输入名称、编号或实体 ID"></label><button class="button${entry.loading ? ' button-pending' : ''}" ${entry.loading ? 'disabled aria-busy="true"' : ''} type="button" data-assignment-search="${index}">搜索</button><label class="property-assignment-wide">所属实体<select data-assignment-select="${index}" ${entry.loading ? 'disabled' : ''}>${[...choices.values()].map(target=>`<option value="${escapeHtml(target.entity.entity_id)}" ${target.entity.entity_id===entry.selected?'selected':''} ${target.selectable?'':'disabled'}>${escapeHtml(assignmentEntityLabel(target.entity,target.identity_properties))}${target.selectable?'':' · 身份冲突'}</option>`).join('')}</select></label>${entry.error?`<p role="alert">${escapeHtml(entry.error)}</p>`:''}${entry.truncated?'<p>仅显示前 20 个匹配实体，请缩小搜索范围。</p>':''}${selected?.evidence?`<details class="property-assignment-wide"><summary>查看所选实体的来源依据</summary><blockquote>${escapeHtml(selected.evidence.quoted_text)}</blockquote></details>`:''}<label class="property-assignment-wide">更改依据<textarea data-assignment-notes="${index}" maxlength="2000" rows="2" placeholder="说明原文中的编号或上下文为何对应所选实体">${escapeHtml(entry.notes || '')}</textarea></label><p class="property-assignment-wide review-muted">只更改这条属性的归属。保存后重新检查，再确认事实；找不到正确实体时可取消并暂缓处理。</p><button class="button primary" type="button" data-assignment-save="${index}" ${entry.loading || entry.error || !selected?.selectable || entry.selected===item.subject.entity_id?'disabled':''}>保存归属并重新检查</button></div>`;
      }
      function renderPropertyAssignment(item) {
        const index=state.reviews.findIndex(value=>value.record_id===item.record_id && value.revision===item.revision);
        const panel=elements.reviewList.querySelector(`[data-property-assignment="${index}"]`);
        if(panel) {panel.innerHTML=propertyAssignmentMarkup(item,index);bindReviewActions(panel);}
        updateReviewAvailability(item);setReviewBusy(Boolean(state.reviewBusy));
      }
      async function loadPropertyAssignment(item,open=false,query='') {
        if(!item || item.record_kind!=='ASSERTION' || item.object_entity) return;
        state.propertyAssignments ||= new Map();
        const old=propertyAssignment(item);
        const entry={revision:item.revision,identityEpoch:state.identityEpoch,reviewEpoch:state.reviewEpoch,
          ...old,open:open || Boolean(old?.open),selected:old?.selected || item.subject.entity_id,query,loading:true,error:null};
        state.propertyAssignments.set(item.record_id,entry);renderPropertyAssignment(item);
        try {
          const result=await apiRequest(`/v1/knowledge/property-assignment/${encodeURIComponent(item.record_id)}?expected_revision=${item.revision}&query=${encodeURIComponent(entry.open ? query : item.subject.entity_id)}`);
          if(propertyAssignment(item)!==entry) return;
          if(result.record_id!==item.record_id || result.revision!==item.revision) throw new Error('记录版本已变化，请刷新审核队列');
          Object.assign(entry,result);
          if(!result.items.some(target=>target.entity.entity_id===entry.selected)) entry.selected=item.subject.entity_id;
        } catch(error) {if(propertyAssignment(item)===entry) entry.error=error.message;}
        finally {if(propertyAssignment(item)===entry) {entry.loading=false;renderPropertyAssignment(item);}}
      }
      async function savePropertyAssignment(index) {
        const item=state.reviews[index], entry=item && propertyAssignment(item);
        if(state.reviewBusy || state.publicationBusy || !entry?.open || entry.loading || entry.error) return;
        if(elements.reviewList.querySelector(`[data-review-editor="${index}"]`)?.disabled===false) {showToast('请先保存或取消内容编辑');return;}
        const target=entry.items?.find(value=>value.entity.entity_id===entry.selected);
        if(!target?.selectable || target.entity.entity_id===item.subject.entity_id) {showToast('请选择需要更改到的实体');return;}
        const notes=entry.notes?.trim();
        if(!notes || notes.length>2000) {showToast('请填写 1–2000 字的更改依据');return;}
        const identityEpoch=state.identityEpoch;
        setReviewBusy(true,[item.record_id]);
        try {
          const result=await apiRequest('/v1/knowledge/property-assignment:apply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({record_id:item.record_id,expected_revision:item.revision,target_entity_id:target.entity.entity_id,target_record_id:target.record_id,target_expected_revision:target.revision,notes})});
          if(identityEpoch!==state.identityEpoch) return;
          trackReviewedOutcomes(result.outcomes || []);invalidatePublicationPreview();
          state.propertyAssignments.delete(item.record_id);state.reviewPhase='paused';
          showToast('属性归属已更改；请在暂缓区核对重新检查结果，再确认事实');
          await loadReviews({anchorRecordId:item.record_id,impact:reviewRefreshImpact(result.outcomes || [],target.entity)});
          if(identityEpoch===state.identityEpoch) void loadPublicationCandidates();
        } catch(error) {if(identityEpoch===state.identityEpoch) showToast(error.message);}
        finally {if(identityEpoch===state.identityEpoch) setReviewBusy(false);}
      }

      function standardizedTitle(item) {
        return item.record_kind === 'ENTITY_MENTION' ? '标准化实体' : item.object_entity ? '标准化关系' : '标准化属性';
      }
      function standardizedReview(item) {
        const evidence_ids = [item.revision_id];
        if (item.record_kind === 'ENTITY_MENTION') return {
          entity_id:item.entity.entity_id, entity_type:item.entity.entity_type,
          standard_name:item.entity.canonical_name, aliases:[...(item.entity.aliases || [])], evidence_ids,
        };
        const raw = reviewEdit(item);
        if (item.object_entity) return {
          relationship_id:item.record_id, source_entity_id:item.subject.entity_id,
          relationship_type:item.predicate, target_entity_id:item.object_entity.entity_id,
          properties:raw.relationship_properties.map(value=>({property_name:value.name,
            value:value.literal.raw_literal, unit:value.literal.raw_unit ?? null,
            valid_from:value.literal.raw_valid_from ?? null, valid_to:value.literal.raw_valid_to ?? null,
            observed_at:value.literal.raw_observed_at ?? null})), evidence_ids,
        };
        return {property_id:item.record_id, entity_id:item.subject.entity_id, property_name:item.predicate,
          value:raw.literal.raw_literal, unit:raw.literal.raw_unit ?? null,
          valid_from:raw.literal.raw_valid_from ?? null, valid_to:raw.literal.raw_valid_to ?? null,
          observed_at:raw.literal.raw_observed_at ?? null, evidence_ids};
      }
      function standardEndpointChoices(item) {
        const rows=(state.publicationCandidates || []).map(value=>value.record).filter(value=>
          value?.record_kind==='ENTITY_MENTION' && ['APPROVED','PUBLISHED'].includes(value.trust?.status) &&
          value.evidence?.chunk_id===item.evidence?.chunk_id);
        const choices=new Map();
        for(const row of rows) choices.set(row.entity.entity_id,{entity:row.entity,revision_id:row.revision_id});
        if(item.subject && !choices.has(item.subject.entity_id)) choices.set(item.subject.entity_id,{entity:item.subject,revision_id:item.subject_mention_revision_id});
        if(item.object_entity && !choices.has(item.object_entity.entity_id)) choices.set(item.object_entity.entity_id,{entity:item.object_entity,revision_id:item.object_mention_revision_id});
        return choices;
      }
      function standardEndpointMarkup(item,index) {
        if(item.record_kind==='ENTITY_MENTION') return '';
        return (item.object_entity ? [['source_entity_id','起点实体',item.subject],['target_entity_id','终点实体',item.object_entity]] : [['entity_id','所属实体',item.subject]]).map(([field,label,current])=>
          `<label>${label}<select data-standard-endpoint="${index}" data-standard-field="${field}" disabled>${[...standardEndpointChoices(item).values()].map(value=>`<option value="${escapeHtml(value.entity.entity_id)}" ${value.entity.entity_id===current.entity_id?'selected':''}>${escapeHtml(value.entity.canonical_name)} · ${escapeHtml(value.entity.entity_type)} · ${escapeHtml(shortId(value.entity.entity_id))}</option>`).join('')}</select></label>`).join('');
      }
      function standardizedEdit(item, value) {
        const initial=standardizedReview(item), edit=reviewEdit(item);
        if (!value || Array.isArray(value) || typeof value!=='object' ||
            Object.keys(value).sort().join('|')!==Object.keys(initial).sort().join('|'))
          throw new Error('请保留标准化 JSON 的字段结构');
        for(const field of [...(item.record_kind==='ENTITY_MENTION'?['entity_id']:[]),'property_id','relationship_id','evidence_ids']) {
          if (field in initial && JSON.stringify(value[field])!==JSON.stringify(initial[field]))
            throw new Error('ID 和原文引用由系统维护；实体归属请通过匹配或关联实体选择修改');
        }
        if(item.record_kind==='ENTITY_MENTION') {
          edit.entity.entity_type=value.entity_type; edit.entity.canonical_name=value.standard_name;
          edit.entity.aliases=value.aliases; return edit;
        }
        const bindEndpoint=(id,role)=>{
          const target=standardEndpointChoices(item).get(id);
          if(!target) throw new Error('请从当前来源的已确认实体中选择关联对象');
          const {entity_type,canonical_key,canonical_name,aliases=[]}=target.entity;
          edit[role==='subject'?'subject':'object_entity']={entity_type,canonical_key,canonical_name,aliases};
          edit[role==='subject'?'subject_mention_revision_id':'object_mention_revision_id']=target.revision_id;
        };
        bindEndpoint(value.entity_id ?? value.source_entity_id,'subject');
        if(item.object_entity) bindEndpoint(value.target_entity_id,'object');
        const literal=(value,original)=>{
          const result={raw_literal:String(value.value)};
          if(original?.source_encoding) result.source_encoding=original.source_encoding;
          for(const field of ['unit','valid_from','valid_to','observed_at'])
            if(value[field]!=null && value[field]!=='') result[`raw_${field}`]=String(value[field]);
          return result;
        };
        if(item.object_entity) {
          edit.predicate=value.relationship_type;
          if(!Array.isArray(value.properties) || value.properties.length!==edit.relationship_properties.length)
            throw new Error('增补关系属性请使用人工补充，已有属性在这里逐项校正');
          edit.relationship_properties=edit.relationship_properties.map((old,index)=>({...old,
            name:value.properties[index].property_name,literal:literal(value.properties[index],old.literal)}));
        } else {edit.predicate=value.property_name;edit.literal=literal(value,edit.literal);}
        return edit;
      }

      function reviewIsEditing(index) {
        return Boolean(state.reviews[index] && propertyAssignment(state.reviews[index])?.open) || elements.reviewList.querySelector(`[data-review-editor="${index}"]`)?.disabled === false;
      }

      function setReviewEditing(index, enabled) {
        const item = state.reviews[index];
        if (!item) return;
        const editor = elements.reviewList.querySelector(`[data-review-editor="${index}"]`);
        const panel = elements.reviewList.querySelector(`[data-review-edit-panel="${index}"]`);
        const toggle = elements.reviewList.querySelector(`[data-review-edit-toggle="${index}"]`);
        if (!editor || !panel || !toggle) return;
        editor.disabled = !enabled;
        elements.reviewList.querySelectorAll(`[data-standard-endpoint="${index}"]`).forEach(select=>{select.disabled=!enabled;});
        panel.hidden = !enabled;
        toggle.textContent = enabled ? '取消编辑' : '编辑内容';
        toggle.setAttribute('aria-expanded', String(enabled));
        elements.reviewList.querySelectorAll(`[data-review-index="${index}"]`).forEach(button => {
          const approve = button.dataset.reviewAction === 'APPROVED';
          button.hidden = enabled;
          if (approve) {
            button.textContent = reviewConfirmLabel(item, button.dataset.factIndependent === 'true');
            button.disabled = enabled || !reviewApproval(item,button.dataset.factIndependent==='true').allowed;
          }
        });
        setReviewBusy(Boolean(state.reviewBusy));
        if (enabled) editor.focus();
        else editor.value = JSON.stringify(standardizedReview(item), null, 2);
      }

      function resolutionMarkup(item, index) {
        if (item.record_kind !== 'ENTITY_MENTION') return '';
        const result=state.resolutions.get(item.record_id);
        const label=(result?.suggestions?.some(value=>value.target) || result?.review_targets?.some(value=>value.selectable)) ? '找到候选，身份尚待确认' : result?.status==='ready' ? '未找到可确认的匹配目标' : '身份尚待确认';
        return `<details class="review-resolution" ${!result || ['queued','loading'].includes(result.status)?'open':''} data-resolution-details="${index}"><summary>实体匹配与消歧 · ${label}</summary>${resolutionContent(item,index)}</details>`;
      }
      function resolutionContent(item, index) {
        if (item.record_kind !== 'ENTITY_MENTION') return '';
        const resolution = state.resolutions.get(item.record_id);
        if (!resolution || resolution.revision !== item.revision) return `<button class="button" type="button" data-resolution-load="${index}">匹配实体</button>`;
        if (['queued', 'loading'].includes(resolution.status)) return `<button class="button button-pending" disabled aria-busy="true" data-resolution-load="${index}">匹配实体</button>`;
        if (resolution.error) return `<div class="output-box">自动匹配失败：${escapeHtml(resolution.error)}。请先保留此记录，恢复后重新匹配。</div><div class="workbench-actions"><button class="button" type="button" data-resolution-load="${index}">重新匹配</button></div>`;
        const outcomeLabels = {AUTO_LINK: '唯一匹配建议 · 待人工确认', REVIEW: '存在相似或多个目标 · 需人工判断', NO_MATCH: '没有匹配目标 · 保留为新实体候选', CONFLICT: '需要人工判断 · 不自动关联'};
        const properties = (resolution.identity_properties || []).map(value => `${escapeHtml(value.name)} = ${escapeHtml(value.canonical_value)}${value.canonical_unit ? ` ${escapeHtml(value.canonical_unit)}` : ''}`).join(' · ');
        const suggestions = (resolution.suggestions || []).map((suggestion, suggestionIndex) => {
          const target = suggestion.target;
          const evidence = suggestion.evidence?.[0];
          const source = evidence?.authoritative_evidence?.[0];
          const canApply = target && ['AUTO_LINK', 'REVIEW'].includes(suggestion.outcome);
          const reason = suggestion.outcome === 'NO_MATCH' ? '当前可访问的权威知识中未找到匹配目标。核对原文后，可确认为独立实体。'
            : suggestion.outcome === 'CONFLICT' ? ((suggestion.reason_code === 'IDENTITY_EVIDENCE_MISSING' || suggestion.reason?.includes('missing identity')) ? '当前信息不足以自动匹配。可依据原文确认为独立实体，也可核对双方原文后归入已有实体。' : '匹配存在不确定性，请核对原文后判断归属；相似候选不要求合并。')
            : ({EXACT_CANONICAL_KEY:'唯一标识与已有实体一致。', EXACT_IDENTITY_PROPERTIES:'本体要求的身份属性全部一致，且只对应一个已有实体。', EXACT_GOVERNED_ALIAS:'名称与已有实体的唯一治理别名一致。', EXACT_CANONICAL_NAME:'名称匹配，请结合原文确认是否为同一实体。', SIMILAR_NAME:'名称相似，仅供人工核对，不能据此直接认定为同一实体。'})[evidence?.match_kind] || '请结合匹配依据及权威来源确认是否为同一实体。';
          return `<div class="review-match"><div class="governance-item-head"><strong>${escapeHtml((suggestion.reason_code === 'IDENTITY_EVIDENCE_MISSING' || suggestion.reason?.includes('missing identity')) ? '身份依据不足 · 需人工确认' : outcomeLabels[suggestion.outcome] || suggestion.outcome)}${target ? ` · ${escapeHtml(target.canonical_name)}` : ''}</strong></div><p>${escapeHtml(reason)}</p>${source ? `<div class="exact-evidence">权威来源：${escapeHtml(source.quoted_text)}</div>` : ''}<details class="review-technical"><summary>匹配依据详情</summary><p>${escapeHtml(suggestion.reason)}<br>rule ${escapeHtml(suggestion.rule_version)} · matcher ${escapeHtml(suggestion.matcher_version)} · confidence ${escapeHtml(suggestion.confidence)}${target ? `<br>${escapeHtml(target.entity_type)} · ${escapeHtml(target.canonical_key)}` : ''}${evidence ? `<br>${escapeHtml(evidence.match_kind)} · ${escapeHtml(evidence.candidate_value)}` : ''}${source ? `<br>Chunk ${escapeHtml(source.chunk_id)} · 字符 ${escapeHtml(source.char_start)}–${escapeHtml(source.char_end)}` : ''}</p></details>${canApply ? `<div class="workbench-actions"><button class="button primary" type="button" data-resolution-apply="${index}" data-resolution-suggestion="${suggestionIndex}">确认使用已有实体</button></div>` : ''}</div>`;
        }).join('');
        return `<div class="review-matching-basis"><strong>自动匹配参考</strong><br>${properties || '未取得可用的自动匹配身份属性；仍可依据原文进行人工身份判断。'}<br>自动匹配不修改候选。确认使用已有实体后，系统会更新相关事实的归属；这些事实仍需到下一阶段单独审核。</div>${suggestions}${manualResolutionMarkup(resolution,index)}<div class="workbench-actions"><button class="button" type="button" data-resolution-load="${index}">刷新匹配</button></div>`;
      }

      function manualResolutionMarkup(resolution,index) {
        const targets=resolution.review_targets || [];
        return `<div class="review-match"><strong>人工选择已有实体</strong><p>包含已确认但尚未发布的实体，以及已发布实体。名称相同只用于查找，请查看双方原文后确认。</p><div class="workbench-actions"><input aria-label="查找已有实体" data-resolution-query="${index}" value="${escapeHtml(resolution.query || '')}" placeholder="输入名称、别名或实体 ID"><button type="button" class="button" data-resolution-search="${index}">查找</button></div>${targets.map((target,targetIndex)=>`<div class="review-match"><strong>${escapeHtml(target.entity.canonical_name)}</strong> · ${target.status==='APPROVED'?'已确认，尚未发布':'已发布'}<p>${escapeHtml(target.entity.entity_type)} · ${escapeHtml(target.entity.canonical_key)}<br>${(target.identity_properties || []).map(value=>`${escapeHtml(value.name)} = ${escapeHtml(value.value)}`).join(' · ')}</p>${reviewEvidence(target.evidence,'查看目标实体原文',target)}<p>${escapeHtml(target.reason)}</p><button type="button" class="button primary" data-resolution-apply="${index}" data-resolution-suggestion="-1" data-resolution-manual="${targetIndex}" data-resolution-blocked="${!target.selectable}" ${target.selectable?'':'disabled'}>确认归入此实体</button></div>`).join('') || '<p>未找到可见的已确认实体，可调整查找条件。</p>'}${resolution.targets_truncated?'<p>结果超过 20 条，请输入更具体的名称或标识。</p>':''}</div>`;
      }

      function revisionHistoryMarkup(item, index) {
        const history = state.revisionHistories.get(item.record_id);
        if (!history || history.headRevision !== item.revision) return `<div class="workbench-actions"><button class="button" type="button" data-revision-history="${index}">查看不可变 revision 历史</button></div>`;
        if (history.error) return `<div class="output-box">${escapeHtml(history.error)}</div><div class="workbench-actions"><button class="button" type="button" data-revision-history="${index}">重试历史</button></div>`;
        const rows = (history.items || []).map(revision => `<div class="exact-evidence"><strong>revision ${escapeHtml(revision.revision)} · ${escapeHtml(revision.trust?.status)}</strong> · ${escapeHtml(revision.revision_id)}<br>${escapeHtml(revision.trust?.reviewed_by || 'unreviewed')}${revision.trust?.reviewed_at ? ` · ${escapeHtml(revision.trust.reviewed_at)}` : ''}${revision.trust?.review_notes ? `<br>${escapeHtml(revision.trust.review_notes)}` : ''}</div>`).join('');
        return `<div class="provider-note" style="margin-top:10px"><strong>不可变 revision 历史</strong><br>按 revision 由新到旧；读取仍执行租户、原文证据与 ACL 校验。</div>${rows}<div class="workbench-actions"><button class="button" type="button" data-revision-history="${index}">刷新历史</button></div>`;
      }

      function reviewModel() {
        state.reviewAssessments ||= new Map();
        state.assessmentQueue ||= [];
        state.assessmentActive ??= 0;
        state.reviewPhase ||= 'identities';
        state.reviewFactTab ||= 'properties';
      }

      function reviewEntity(item) { return item.entity || item.subject || {}; }
      function reviewGroupKey(item) { return reviewEntity(item).entity_id || `record:${item.record_id}`; }
      function reviewKindLabel(item) { return item.record_kind === 'ENTITY_MENTION' ? '实体提及' : item.object_entity ? '关系' : '属性'; }
      function reviewPropertyLabel(predicate) {
        return ({EquipmentCode:'设备编码', RatedPower:'额定功率', CONTAINS:'包含部件', INSTALLED_AT:'安装于', EXPOSED_TO:'存在风险', OPERATES:'运营', SUPPLIED_BY:'供应方'})[predicate] || predicate;
      }
      function reviewFactText(item) {
        const literal = literalSemantics(item);
        const value = item.object_entity?.canonical_name ?? literal.canonical_value ?? literal.raw_value ?? item.literal_value ?? '';
        const unit = item.object_entity ? '' : literal.canonical_unit || literal.raw_unit || '';
        return `${reviewPropertyLabel(item.predicate)}：${value}${unit ? ` ${unit}` : ''}`;
      }
      function reviewTimeText(item) {
        const value=literalSemantics(item);
        if(!item.literal_semantics && item.object_entity) return '';
        return `适用起点：${value.valid_from || '未声明'}；终点：${value.valid_to || '未声明'}；观测时间：${value.observed_at || '未声明'}`;
      }
      function reviewEvidence(evidence, label = '查看原文证据', record = null, evidenceRole = 'PRIMARY') {
        if (!evidence) return '<span class="review-muted">来源证据暂不可用</span>';
        const context=evidenceRole==='PRIMARY'?record?.context_property_evidence:null;
        const source=record ? ` data-evidence-record="${escapeHtml(record.record_id)}" data-evidence-revision="${escapeHtml(record.revision)}"${evidenceRole==='CONTEXT_VALUE'?' data-evidence-role="CONTEXT_VALUE"':''}` : '';
        const markup=`<details class="review-evidence"${source}><summary>${escapeHtml(context?.value_evidence?'实体原文':label)}</summary><div data-evidence-context><blockquote>${escapeHtml(evidence.quoted_text || '此视图仅提供来源定位')}</blockquote><small>Chunk ${escapeHtml(shortId(evidence.chunk_id))} · 字符 ${escapeHtml(evidence.char_start)}–${escapeHtml(evidence.char_end)}</small></div></details>`;
        if(!context?.value_evidence)return markup;
        return `<div class="context-property-evidence">${markup}${reviewEvidence(context.value_evidence,'上下文字段原文',record,'CONTEXT_VALUE')}<details class="review-technical"><summary>查看上下文绑定位置</summary><p>字段：${escapeHtml(context.value_pointer || '')}<br>作用域：${escapeHtml(context.scope_pointer || '')}<br>对象记录：${escapeHtml(context.record_pointer || '')}<br>对象标识：${escapeHtml(context.identity_pointer || '')}</p></details></div>`;
      }
      function evidenceContextMarkup(value) {
        const characters=Array.from(value.text);
        const start=Math.max(0,Math.min(characters.length,value.char_start-value.context_start));
        const end=Math.max(start,Math.min(characters.length,value.char_end-value.context_start));
        const body=escapeHtml(characters.slice(0,start).join(''))+(end>start ? `<mark>${escapeHtml(characters.slice(start,end).join(''))}</mark>`:'')+escapeHtml(characters.slice(end).join(''));
        return `<p><strong>${escapeHtml(value.document_title)}</strong></p><blockquote style="white-space:pre-wrap">${body}</blockquote><small>文档版本 ${escapeHtml(shortId(value.version_id))} · Chunk ${escapeHtml(shortId(value.chunk_id))} · 命中字符 ${value.char_start}–${value.char_end}<br>当前显示字符 ${value.context_start}–${value.context_end}${value.source_uri ? `<br>${escapeHtml(value.source_uri)}`:''}</small><div class="workbench-actions"><button type="button" class="button" data-evidence-view="paragraph">所在段落</button><button type="button" class="button" data-evidence-view="surrounding">展开前后文</button>${value.document_accessible ? `<button type="button" class="button" data-evidence-view="document" data-evidence-offset="${Math.floor(value.char_start/8000)*8000}">查看完整文档</button>`:'<span>仅显示当前可访问切块</span>'}${value.view==='document' && value.has_previous ? `<button type="button" class="button" data-evidence-view="document" data-evidence-offset="${Math.max(0,value.context_start-8000)}">上一页</button>`:''}${value.view==='document' && value.has_next ? `<button type="button" class="button" data-evidence-view="document" data-evidence-offset="${value.context_end}">下一页</button>`:''}</div>`;
      }
      async function loadEvidenceContext(details, view='paragraph', offset=0) {
        const panel=details.querySelector('[data-evidence-context]');
        const identityEpoch=state.identityEpoch, request=(details.contextRequest || 0)+1;
        details.contextRequest=request;
        panel.innerHTML='<button class="button button-pending" disabled aria-busy="true">读取原文</button>';
        try {
          const value=await apiRequest('/v1/knowledge/review-evidence',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({record_id:details.dataset.evidenceRecord,expected_revision:Number(details.dataset.evidenceRevision),view,offset,...(details.dataset.evidenceRole==='CONTEXT_VALUE'?{evidence_role:'CONTEXT_VALUE'}:{})})});
          if(identityEpoch!==state.identityEpoch || details.contextRequest!==request || details.isConnected===false) return;
          panel.innerHTML=evidenceContextMarkup(value);
          panel.querySelectorAll('[data-evidence-view]').forEach(button=>button.addEventListener('click',foregroundAction('', ()=>loadEvidenceContext(details,button.dataset.evidenceView,Number(button.dataset.evidenceOffset || 0)))));
        } catch(error) {
          if(identityEpoch!==state.identityEpoch || details.contextRequest!==request || details.isConnected===false) return;
          panel.innerHTML=`<p>无法读取原文：${escapeHtml(error.message)}</p><button type="button" class="button" data-evidence-retry>重试</button>`;
          panel.querySelector('[data-evidence-retry]')?.addEventListener('click',foregroundAction('', ()=>loadEvidenceContext(details,view,offset)));
        }
      }
      function bindEvidenceActions(container) {
        container.querySelectorAll('[data-evidence-record]').forEach(details=>details.addEventListener('toggle',()=> {
          if(details.open && !details.contextRequest) void loadEvidenceContext(details);
        }));
      }
      function reviewAssessment(item) {
        reviewModel();
        const result = state.reviewAssessments.get(item.record_id);
        return result?.revision === item.revision && result.identityEpoch === state.identityEpoch && result.reviewEpoch === state.reviewEpoch ? result : null;
      }
      function reviewApproval(item, independentFact = false) {
        if (item.record_kind === 'ENTITY_MENTION') {
          const result = state.resolutions.get(item.record_id);
          if(result?.error) return {allowed:false,note:'无法读取审核依据，请刷新匹配后重试'};
          if (!result || result.revision !== item.revision || result.identityEpoch !== state.identityEpoch || result.reviewEpoch !== state.reviewEpoch || result.status !== 'ready') return {allowed:false, note:''};
          const permission=result.identity_actions?.independent;
          if(permission) return {allowed:permission.allowed === true, note:permission.note};
          if (result.suggestions?.some(value => value.outcome === 'NO_MATCH')) return {allowed:true, note:'核对原文后，可确认为独立实体'};
          if (result.suggestions?.some(value => value.target)) return {allowed:false, note:'请先确认使用已有实体'};
          return {allowed:false, note:'身份依据不足或冲突，请暂缓处理'};
        }
        const result = reviewAssessment(item);
        return {allowed:(independentFact?['READY','DUPLICATE','CONFLICT']:['READY','DUPLICATE']).includes(result?.status), note:result?.summary || result?.error || ''};
      }
      function reviewTechnical(item, index) {
        return `<details class="review-technical" data-review-details="${index}"><summary>${standardizedTitle(item)}</summary>${standardEndpointMarkup(item,index)}<textarea id="review-editor-${index}" class="json-editor compact" data-review-editor="${index}" disabled spellcheck="false" aria-label="${standardizedTitle(item)} JSON">${escapeHtml(JSON.stringify(standardizedReview(item), null, 2))}</textarea><button class="button" type="button" data-review-edit-toggle="${index}" aria-expanded="false" aria-controls="review-editor-${index}">编辑内容</button><div data-review-edit-panel="${index}" hidden><p>修改名称、类型或事实内容后保存，系统重新校验再确认。ID、原文引用和来源等级由系统维护；文档之外的事实请使用人工补充。</p><button class="button" type="button" data-review-save-draft="${index}">保存修改并重新检查</button></div><details><summary>修订历史</summary><div data-review-history-panel="${index}">${revisionHistoryMarkup(item, index)}</div></details></details>`;
      }
      function reviewConfirmLabel(item, independent = false) {
        if (item.record_kind === 'ENTITY_MENTION') return '确认为独立实体';
        return independent ? '作为独立事实确认' : '确认事实';
      }
      function reviewActions(item, index) {
        const check = reviewApproval(item);
        const paused = item.trust?.status === 'QUARANTINED';
        const approveLabel = reviewConfirmLabel(item);
        return `<p class="provider-note" role="status" data-review-note="${index}">${escapeHtml(check.note)}</p><div class="workbench-actions review-actions"><button class="button primary" type="button" data-review-action="APPROVED" data-review-index="${index}" ${check.allowed ? '' : 'disabled'} title="${escapeHtml(check.note)}">${approveLabel}</button>${item.record_kind === 'ASSERTION' ? `<button class="button" type="button" data-review-action="APPROVED" data-fact-independent="true" data-review-index="${index}" ${reviewApproval(item,true).allowed?'':'disabled'}>作为独立事实确认</button>` : ''}${item.record_kind === 'ENTITY_MENTION' ? `<button class="button" type="button" data-review-existing="${index}">归入已有实体</button>` : ''}<button class="button danger" type="button" data-review-action="REJECTED" data-review-index="${index}">不采纳</button><button class="button" type="button" data-review-action="QUARANTINED" data-review-index="${index}" ${paused ? 'disabled' : ''}>${paused ? '已暂缓' : '暂缓处理'}</button></div>`;
      }
      function reviewSelection(item, index) {
        const check = reviewApproval(item);
        return `<input type="checkbox" data-review-select="${index}" aria-label="选择${escapeHtml(reviewEntity(item).canonical_name)}的${escapeHtml(reviewKindLabel(item))}" title="${escapeHtml(check.note)}">`;
      }
      function assessmentMarkup(item, index) {
        const result = reviewAssessment(item);
        if (!result) return `<button class="button" type="button" data-review-assess="${index}">检查事实</button>`;
        if (['queued','loading'].includes(result.status)) return `<button class="button button-pending" disabled aria-busy="true" data-review-assess="${index}">检查事实</button>`;
        if (result.error) return `<div class="review-check blocked">检查未完成：${escapeHtml(result.error)}。请先保留此记录。</div><button class="button" data-review-assess="${index}">重新检查</button>`;
        const labels = {READY:'可以审核', BLOCKED:'先处理关联实体', DUPLICATE:'已有相同的已发布事实', CONFLICT:'存在差异，需要核对', UNAVAILABLE:'检查信息不完整'};
        const dependencies = (result.dependencies || []).filter(value => !value.ready).map(value => `<li>${escapeHtml(value.role === 'subject' || value.role === 'SUBJECT' ? '所属实体' : '关联实体')}“${escapeHtml(value.name)}”：尚未确认${value.mention_record_id ? ` <button class="button link" type="button" data-review-dependency="${escapeHtml(value.mention_record_id)}">去处理这个实体</button>` : ''}</li>`).join('');
        const matches = (result.matches || []).map(match => `<div class="review-comparison"><strong>已有事实：${escapeHtml(reviewFactText(match.record))} · ${match.record.trust?.authority==='AUTHORITATIVE'?'权威来源':'业务来源'}</strong>${reviewEvidence(match.record.evidence, '查看已发布来源')}<p>${escapeHtml(reviewTimeText(match.record))}</p><details><summary>规范化值与适用时间</summary><pre>${escapeHtml(JSON.stringify(match.record.literal_semantics || match.record.relationship_properties || [], null, 2))}</pre></details></div>`).join('');
        return `${item.fact_distinction?`<div class="review-muted"><strong>人工独立事实 · 区分依据尚未结构化</strong><p>${escapeHtml(item.fact_distinction.reason)}</p><small>${escapeHtml(item.fact_distinction.reviewed_by)} · ${escapeHtml(item.fact_distinction.reviewed_at)}；以上为审核判断，不是原文事实。</small></div>`:''}<div class="review-check ${result.status.toLowerCase()}"><strong>${escapeHtml(labels[result.status] || result.status)}</strong><p>${escapeHtml(result.summary)}</p>${dependencies ? `<ul>${dependencies}</ul>` : ''}</div>${matches}${result.status === 'DUPLICATE' ? `<p>确认后本次来源进入待发布列表；${item.object_entity?'发布后同一关系仅显示一条连线，':''}各条来源的原文、等级和审核记录分别保留。</p>` : ''}${result.status === 'CONFLICT' ? '<p class="review-muted">当前来源与已发布来源存在差异。请核对原文；若属于独立事实，可填写区分理由后单独确认。不会自动覆盖已有知识。</p>' : ''}<button class="button review-refresh" type="button" data-review-assess="${index}">重新检查</button>`;
      }
      function reviewProgress() {
        const active = state.reviews.filter(item => item.trust?.status !== 'QUARANTINED');
        const identities = active.filter(item => item.record_kind === 'ENTITY_MENTION').length;
        const facts = active.length - identities;
        const paused = state.reviews.length - active.length;
        return {identities, facts, paused};
      }
      function stableReviewOrder(previous, incoming) {
        // Ordering is presentation only: identity is still the stable entity ID.
        const groups=new Map(), records=new Map(previous.map((item,index)=>[item.record_id,index]));
        for(const item of [...previous,...incoming]) if(!groups.has(reviewGroupKey(item))) groups.set(reviewGroupKey(item),groups.size);
        return [...incoming].sort((a,b)=>groups.get(reviewGroupKey(a))-groups.get(reviewGroupKey(b))
          || (records.get(a.record_id) ?? Infinity)-(records.get(b.record_id) ?? Infinity));
      }
      function captureReviewViewport(preferredRecordId=null) {
        const rows=[...elements.reviewList.querySelectorAll('[data-review-record]')];
        const visible=row=>{const rect=row.getBoundingClientRect?.();return rect && rect.bottom>100 && rect.top<(globalThis.innerHeight || 900);};
        const row=rows.find(row=>row.dataset.reviewRecord===preferredRecordId && visible(row)) || rows.find(visible);
        if(!row) return null;
        const card=row.closest?.('[data-review-group-key]');
        return {recordId:row.dataset.reviewRecord,groupKey:card?.dataset.reviewGroupKey,
          top:row.getBoundingClientRect().top,cardTop:card?.getBoundingClientRect().top,
          peers:[...(card?.querySelectorAll('[data-review-record]') || [])].map(peer=>peer.dataset.reviewRecord),
          following:rows.slice(rows.indexOf(row)+1).map(peer=>peer.dataset.reviewRecord)};
      }
      function restoreReviewViewport(anchor) {
        if(!anchor) return;
        const rows=[...elements.reviewList.querySelectorAll('[data-review-record]')];
        let target=rows.find(row=>row.dataset.reviewRecord===anchor.recordId), top=anchor.top;
        if(!target) {
          target=rows.find(row=>anchor.peers.includes(row.dataset.reviewRecord));
          if(target) top=Math.max(110,Math.min(anchor.top,(globalThis.innerHeight || 900)-160));
        }
        if(!target) {
          target=rows.find(row=>anchor.following.includes(row.dataset.reviewRecord));
          top=110;
        }
        if(!target) target=[...elements.reviewList.querySelectorAll('[data-review-group-key]')].find(card=>card.dataset.reviewGroupKey===anchor.groupKey);
        if(!target) return;
        const delta=target.getBoundingClientRect().top-top;
        if(Math.abs(delta)>1) globalThis.scrollBy?.({top:delta,left:0,behavior:'instant'});
      }
      function captureReviewUi() {
        const saved = new Map();
        elements.reviewList.querySelectorAll('[data-review-record]').forEach(row => {
          const editor = row.querySelector('[data-review-editor]');
          const select = row.querySelector('[data-review-select]');
          saved.set(`${row.dataset.reviewRecord}:${row.dataset.reviewRevision}`, {value:editor?.value, editing:editor && !editor.disabled, selected:select?.checked,
            details:row.querySelector('[data-review-details]')?.open, resolution:row.querySelector('[data-resolution-details]')?.open,
            evidence:[...row.querySelectorAll('.review-evidence')].map(details=>details.open)});
        });
        return saved;
      }
      function disconnectReviewDetails() {
        state.reviewDetailsObserver?.disconnect();
        state.reviewDetailsObserver=null;
      }
      function reviewRowVisible(row) {
        if(!row || row.isConnected===false || row.closest?.('[hidden]')) return false;
        const rect=row.getBoundingClientRect?.();
        return Boolean(rect && rect.width>0 && rect.height>0 && rect.bottom>0 && rect.right>0
          && rect.top<(globalThis.innerHeight || 900) && rect.left<(globalThis.innerWidth || 1440));
      }
      function observeReviewDetails() {
        disconnectReviewDetails();
        // Hidden construction steps never intersect. Browsers without this API
        // keep the explicit check buttons, rather than prefetching every record.
        if(typeof globalThis.IntersectionObserver !== 'function') return;
        const identityEpoch=state.identityEpoch, reviewEpoch=state.reviewEpoch;
        const observer=new globalThis.IntersectionObserver(entries=>{
          if(state.reviewDetailsObserver!==observer || identityEpoch!==state.identityEpoch
            || reviewEpoch!==state.reviewEpoch || state.reviewLoading) return;
          for(const entry of entries) {
            if(!entry.isIntersecting || !reviewRowVisible(entry.target)) continue;
            const item=state.reviews.find(item=>item.record_id===entry.target.dataset.reviewRecord
              && item.revision===Number(entry.target.dataset.reviewRevision));
            if(!item) continue;
            if(item.record_kind==='ENTITY_MENTION') void queueResolution(item,{automatic:true,row:entry.target});
            else void queueAssessment(item,false,{automatic:true,row:entry.target});
          }
        },{root:null,rootMargin:'0px',threshold:0});
        state.reviewDetailsObserver=observer;
        elements.reviewList.querySelectorAll('[data-review-record]').forEach(row=>observer.observe(row));
      }
      function autoReviewReasonMarkup(item) {
        const decision=state.latestAutoReview?.items.find(value=>value.record_id===item.record_id && value.decision!=='AUTO_APPROVED');
        // A receipt is historical evidence, not a current finding after edits or rebinding.
        if(!decision?.reason || !decision.input_revision || decision.input_revision!==item.revision
          || item.trust?.status!=='CANDIDATE')return '';
        const label={NEEDS_HUMAN:'模型存疑，待人工复核',BLOCKED:'等待实体身份确认',INCOMPLETE:'自动审核未完成'}[decision.decision];
        return `<p class="provider-note"><strong>${escapeHtml(label)}：</strong>${escapeHtml(decision.reason)}</p>`;
      }

      function reviewPaginationMarkup() {
        const back=state.reviewPageBack || [];
        const busy=state.reviewLoading || state.reviewBusy || state.publicationBusy;
        return `<div class="workbench-actions" data-review-pagination><span>第 ${back.length+1} 页 · 本页 ${state.reviews.length} 条</span><button class="button" type="button" data-review-page="first" ${busy || !back.length?'disabled':''}>返回首页</button><button class="button" type="button" data-review-page="previous" ${busy || !back.length?'disabled':''}>上一页</button><button class="button" type="button" data-review-page="next" ${busy || !state.reviewNextCursor?'disabled':''}>下一页</button></div>`;
      }

      function renderReviews({anchor=null}={}) {
        disconnectReviewDetails();
        reviewModel();
        const saved = captureReviewUi();
        const progress = reviewProgress();
        const approvedAvailable=state.approvedRevisions.size || state.publicationCandidates?.some(item=>item.record?.trust?.status==='APPROVED');
        const mappingGaps=(state.latestAutoReview?.issues?.length || 0)+(state.latestAutoReview?.context_mapping?.issues?.length || 0);
        if (!state.reviews.length) {
          elements.reviewList.innerHTML = state.reviewPageCursor
            ? '<div class="build-empty-state"><h3>当前页已没有待审核记录。</h3><p>可返回首页查看其他待办。</p></div>'
            : mappingGaps
            ? '<div class="build-empty-state review-mapping-gap"><h3>当前没有待审核记录，仍有必填字段缺口。</h3><p>上方列出了待补字段及影响实体。请修正来源映射或资料后重新构建，再检查发布预览。</p><button class="button" type="button" data-review-upload>返回上传资料</button></div>'
            : approvedAvailable || (state.latestAutoReview?.status==='COMPLETED' && state.latestAutoReview.counts.approved_mentions+state.latestAutoReview.counts.approved_assertions>0)
            ? '<div class="build-empty-state review-complete"><h3>当前批次已完成复核。</h3><p>已确认内容可进入发布预览，核对后再发布。</p><button class="button primary" type="button" data-review-next>下一步：发布知识</button></div>'
            : state.constructionJobs?.[0]?.extraction_mode === 'SOURCE_ONLY'
            ? '<div class="build-empty-state"><h3>最近一次上传仅保存了来源，尚未抽取实例。</h3><p>因此这里没有候选。请返回上传资料，在“来源信息与处理设置”中选择“按本体抽取实体、属性和关系”，完成构建后再复核。</p><button class="button primary" type="button" data-review-upload>返回构建资料</button></div>'
            : '<div class="build-empty-state"><h3>当前没有待确认记录。</h3><p>请先上传文档并抽取，或填写人工补充，再回来核对来源与事实。</p><div class="workbench-actions"><button class="button primary" type="button" data-review-upload>去上传资料</button><button class="button" type="button" data-review-manual>人工补充</button></div></div>';
          elements.reviewList.innerHTML += reviewPaginationMarkup();
          bindReviewActions(elements.reviewList);
          updateReviewBulkActions();
          return;
        }
        if (!progress.identities && state.reviewPhase === 'identities') state.reviewPhase = progress.facts ? 'facts' : 'paused';
        const phase = state.reviewPhase;
        const visible = state.reviews.map((item,index) => ({item,index})).filter(({item}) => phase === 'paused' ? item.trust?.status === 'QUARANTINED' : item.trust?.status !== 'QUARANTINED' && (phase === 'identities' ? item.record_kind === 'ENTITY_MENTION' : item.record_kind !== 'ENTITY_MENTION' && (state.reviewFactTab === 'relationships' ? Boolean(item.object_entity) : !item.object_entity)));
        const groups = new Map();
        for (const row of visible) { const key=reviewGroupKey(row.item); if (!groups.has(key)) groups.set(key,[]); groups.get(key).push(row); }
        const guidance = progress.identities ? '下一步：处理尚未确定的实体身份，再核查剩余事实。'
          : progress.facts ? '实体身份已处理，请核查下面尚未通过的事实。'
          : mappingGaps ? '已生成的可处理记录已审核；请先处理上方必填字段缺口。'
          : approvedAvailable ? '当前批次可处理记录已审核；暂缓记录不参与发布。'
          : progress.paused ? '当前只有暂缓记录，请补充核查；本次没有已批准的待发布内容。'
          : '当前没有待确认记录。请先上传文档并抽取，或填写人工补充。';
        const navigation = `<div class="review-guide"><strong>${guidance}</strong><p>本页待确认提及 ${progress.identities} · 待处理事实 ${progress.facts} · 已暂缓 ${progress.paused} · 本次已确认记录 ${state.approvedRevisions.size}</p><div class="review-phases" role="group" aria-label="审核顺序">${[['identities','1 确认实体身份',progress.identities],['facts','2 审核属性与关系',progress.facts],['paused','暂缓处理',progress.paused]].map(([key,label,count]) => `<button class="button ${phase === key ? 'primary' : ''}" type="button" data-review-phase="${key}" aria-pressed="${phase === key}">${label}（${count}）</button>`).join('')}</div><p class="review-muted">这里列出当前账号可处理的记录，权威等级和来源逐条标明。相同名称不会自动合并；实体确认不会自动批准它的属性或关系。确认或修改不会提升等级。超出原文的事实请使用“人工补充”，不要改写文档证据。</p></div>`;
        const factTabs=phase==='facts' ? `<div class="review-phases" role="group" aria-label="事实类型">${[['properties','属性'],['relationships','关系']].map(([key,label])=>`<button class="button ${state.reviewFactTab===key?'primary':''}" type="button" data-review-fact-tab="${key}" aria-pressed="${state.reviewFactTab===key}">${label}（${state.reviews.filter(item=>item.record_kind!=='ENTITY_MENTION' && item.trust?.status!=='QUARANTINED' && (key==='relationships'?Boolean(item.object_entity):!item.object_entity)).length}）</button>`).join('')}</div>` : '';
        const cards = [...groups.values()].map(rows => {
          const entity = reviewEntity(rows[0].item);
          const mentions = rows.filter(row=>row.item.record_kind === 'ENTITY_MENTION');
          const facts = rows.filter(row=>row.item.record_kind !== 'ENTITY_MENTION');
          const identityRows = mentions.map(({item,index},position) => `<div class="review-mention" id="review-record-${escapeHtml(item.record_id)}" data-review-record="${escapeHtml(item.record_id)}" data-review-revision="${item.revision}"><div class="review-row-head">${reviewSelection(item,index)}<strong>来源提及 ${position+1}</strong><span class="trust-badge">${item.trust?.status === 'QUARANTINED' ? '已暂缓' : '待确认'}</span></div>${provenanceBadges(item.trust)}<div data-auto-review-reason="${escapeHtml(item.record_id)}">${autoReviewReasonMarkup(item)}</div>${reviewEvidence(item.evidence, item.trust?.origin === 'HUMAN_SUPPLEMENT' ? '查看人工补充记录' : '查看文档原文', item)}<div data-resolution-panel="${index}">${resolutionMarkup(item,index)}</div>${reviewTechnical(item,index)}${reviewActions(item,index)}</div>`).join('');
          const factRows = facts.map(({item,index}) => `<tr id="review-record-${escapeHtml(item.record_id)}" data-review-record="${escapeHtml(item.record_id)}" data-review-revision="${item.revision}"><td>${reviewSelection(item,index)}</td><td><strong>${escapeHtml(reviewFactText(item))}</strong><p class="review-muted">${reviewKindLabel(item)} · ${item.trust?.status === 'QUARANTINED' ? '已暂缓' : '待审核'}</p><div class="property-assignment" data-property-assignment="${index}">${propertyAssignmentMarkup(item,index)}</div>${provenanceBadges(item.trust)}<div data-auto-review-reason="${escapeHtml(item.record_id)}">${autoReviewReasonMarkup(item)}</div>${reviewEvidence(item.evidence, item.trust?.origin === 'HUMAN_SUPPLEMENT' ? '查看人工补充记录' : '查看文档原文', item)}<small>${escapeHtml(reviewTimeText(item))}</small>${reviewTechnical(item,index)}</td><td><div data-assessment-panel="${index}">${assessmentMarkup(item,index)}</div>${reviewActions(item,index)}</td></tr>`).join('');
          return `<article class="review-entity-card" data-review-group-key="${escapeHtml(reviewGroupKey(rows[0].item))}"><header><h3>${escapeHtml(entity.canonical_name || '未命名实体')}</h3><small>实体 ID：${escapeHtml(entity.entity_id || '尚未确定')}</small><span class="trust-badge">${escapeHtml(entity.entity_type || '实体')}</span>${mentions.length ? '<span class="trust-badge">待确认分组</span>' : ''}<span>${mentions.length ? `${mentions.length} 条来源提及` : `${facts.length} 条属性或关系`}</span></header>${mentions.length ? '<p class="provider-note">同组仍是候选归属，请根据原文决定共同绑定或拆分；相同名称不代表同一实体。</p>' : ''}${identityRows}${factRows ? `<div class="review-table-wrap"><table class="review-facts"><thead><tr><th>选择</th><th>本次抽取内容与来源</th><th>检查结果与下一步</th></tr></thead><tbody>${factRows}</tbody></table></div>` : ''}</article>`;
        }).join('');
        const identityBatch=state.reviewPhase==='identities' && progress.identities ? '<div class="provider-note">确认所选提及时，默认分别建立独立实体。若所选提及描述同一对象，可使用下面的合并建档操作。<div class="workbench-actions"><button class="button" type="button" data-review-group>所选提及归为同一独立实体</button></div></div>' : '';
        elements.reviewList.innerHTML = navigation + reviewPaginationMarkup() + identityBatch + factTabs + (state.reviews.length >= 100 ? '<div class="provider-note">每页最多显示 100 条，可翻页查看后续待办；暂缓记录也计入本页。<button class="button" data-review-pending-only>只加载待审核记录</button></div>' : '') + (cards || '<div class="output-box">此阶段没有待处理记录。</div>') + (!progress.identities && !progress.facts && approvedAvailable && !mappingGaps ? '<div class="review-complete"><p>当前批次可处理记录已审核。点击下一步检查待发布内容并明确发布；暂缓记录仍需核查。</p><button class="button primary" type="button" data-review-next>下一步：发布知识</button></div>' : '');
        bindReviewActions(elements.reviewList);
        state.reviews.forEach((item,index) => {
          const value = saved.get(`${item.record_id}:${item.revision}`);
          if (!value) return;
          const editor = elements.reviewList.querySelector(`[data-review-editor="${index}"]`);
          const select = elements.reviewList.querySelector(`[data-review-select="${index}"]`);
          if (select && !select.disabled) select.checked = value.selected;
          if (editor && value.editing) { setReviewEditing(index,true); editor.value=value.value; }
          const details=elements.reviewList.querySelector(`[data-review-details="${index}"]`);
          if (details) details.open=value.details || value.editing;
          const row=elements.reviewList.querySelector(`[data-review-record="${item.record_id}"]`);
          const resolution=row?.querySelector('[data-resolution-details]');
          if(resolution) resolution.open=Boolean(value.resolution);
          row?.querySelectorAll('.review-evidence').forEach((details,position)=>{details.open=Boolean(value.evidence?.[position]);});
        });
        setReviewBusy(Boolean(state.reviewBusy));
        restoreReviewViewport(anchor);
        observeReviewDetails();
      }
      function openResolutionChoices(index) {
        if(state.reviewBusy || state.publicationBusy || reviewIsEditing(index)) {showToast('请先完成当前保存或编辑');return;}
        const details=elements.reviewList.querySelector(`[data-resolution-details="${index}"]`);
        if(details) {details.open=true;details.scrollIntoView?.({block:'nearest',behavior:'smooth'});}
      }
      function bindReviewActions(container) {
        container.querySelectorAll('[data-assignment-toggle]').forEach(button=>button.addEventListener('click',()=>{
          const index=Number(button.dataset.assignmentToggle),item=state.reviews[index];
          if(state.reviewBusy || state.publicationBusy) return;
          if(elements.reviewList.querySelector(`[data-review-editor="${index}"]`)?.disabled===false) {showToast('请先保存或取消内容编辑');return;}
          const entry=propertyAssignment(item);
          if(entry?.open) {entry.open=false;entry.selected=item.subject.entity_id;entry.notes='';renderPropertyAssignment(item);}
          else void loadPropertyAssignment(item,true);
        }));
        container.querySelectorAll('[data-assignment-search]').forEach(button=>button.addEventListener('click',()=>{
          const item=state.reviews[Number(button.dataset.assignmentSearch)];
          if(!state.reviewBusy && !state.publicationBusy) void loadPropertyAssignment(item,true,propertyAssignment(item)?.query || '');
        }));
        container.querySelectorAll('[data-assignment-query], [data-assignment-notes]').forEach(input=>input.addEventListener('input',()=>{
          const index=Number(input.dataset.assignmentQuery ?? input.dataset.assignmentNotes),entry=propertyAssignment(state.reviews[index]);
          if(entry) entry[input.dataset.assignmentQuery===undefined?'notes':'query']=input.value;
        }));
        container.querySelectorAll('[data-assignment-select]').forEach(select=>select.addEventListener('change',()=>{
          const item=state.reviews[Number(select.dataset.assignmentSelect)],entry=propertyAssignment(item);
          if(entry && !state.reviewBusy && !state.publicationBusy) {entry.selected=select.value;renderPropertyAssignment(item);}
        }));
        container.querySelectorAll('[data-assignment-save]').forEach(button=>button.addEventListener('click',foregroundAction('', ()=>savePropertyAssignment(Number(button.dataset.assignmentSave)))));
        container.querySelectorAll('[data-review-upload]').forEach(button=>button.addEventListener('click',foregroundAction('', ()=>showConstructionFlow(state.constructionFlow,'source-upload-slot'))));
        container.querySelectorAll('[data-review-manual]').forEach(button=>button.addEventListener('click',()=>{
          const panel=$('manual-fact-panel');panel.open=true;panel.scrollIntoView({behavior:'smooth',block:'nearest'});$('manual-kind').focus({preventScroll:true});
        }));
        container.querySelectorAll('[data-review-select]').forEach(input=>input.addEventListener('change',updateReviewBulkActions));
        container.querySelectorAll('[data-review-existing]').forEach(button=>button.addEventListener('click',foregroundAction('', ()=>openResolutionChoices(Number(button.dataset.reviewExisting)))));
        container.querySelectorAll('[data-review-group]').forEach(button=>button.addEventListener('click',foregroundAction('', ()=>submitReviews('APPROVED',chosenReviews(),false,true))));
        container.querySelectorAll('[data-standard-endpoint]').forEach(select=>select.addEventListener('change',()=>{
          const editor=elements.reviewList.querySelector(`[data-review-editor="${select.dataset.standardEndpoint}"]`);
          try {const value=JSON.parse(editor.value);value[select.dataset.standardField]=select.value;editor.value=JSON.stringify(value,null,2);}
          catch(_) {showToast('请先修正 JSON 格式，再选择关联实体');}
        }));
        container.querySelectorAll('[data-review-action]').forEach(button => button.addEventListener('click', foregroundAction('', () => submitReviews(button.dataset.reviewAction,[Number(button.dataset.reviewIndex)],button.dataset.saveEdits === 'true',false,button.dataset.factIndependent === 'true'))));
        container.querySelectorAll('[data-review-edit-toggle]').forEach(button => button.addEventListener('click', () => {
          if(state.reviewBusy || state.publicationBusy) {showToast('审核或发布正在保存，请稍候');return;}
          if(propertyAssignment(state.reviews[Number(button.dataset.reviewEditToggle)])?.open) {showToast('请先保存或取消归属更改');return;}
          setReviewEditing(Number(button.dataset.reviewEditToggle),button.getAttribute('aria-expanded') !== 'true');
        }));
        container.querySelectorAll('[data-revision-history]').forEach(button => button.addEventListener('click', foregroundAction('', () => loadRevisionHistory(Number(button.dataset.revisionHistory),button))));
        container.querySelectorAll('[data-review-phase]').forEach(button => button.addEventListener('click', () => {
          if ([...elements.reviewList.querySelectorAll('[data-review-editor]')].some(editor=>!editor.disabled)) { showToast('请先保存或取消当前编辑'); return; }
          state.reviewPhase=button.dataset.reviewPhase; renderReviews();
        }));
        container.querySelectorAll('[data-review-fact-tab]').forEach(button=>button.addEventListener('click',()=> {
          if([...elements.reviewList.querySelectorAll('[data-review-editor]')].some(editor=>!editor.disabled)) {showToast('请先保存或取消当前编辑');return;}
          state.reviewFactTab=button.dataset.reviewFactTab;renderReviews();
        }));
        container.querySelectorAll('[data-review-save-draft]').forEach(button=>button.addEventListener('click',foregroundAction('', ()=>submitReviews('QUARANTINED',[Number(button.dataset.reviewSaveDraft)],true))));
        container.querySelectorAll('[data-review-pending-only]').forEach(button=>button.addEventListener('click',()=>loadReviews({candidatesOnly:true,pageCursor:null,pageBack:[]})));
        container.querySelectorAll('[data-review-page]').forEach(button=>button.addEventListener('click',()=> {
          if(state.reviewLoading || state.reviewBusy || state.publicationBusy)return;
          if([...elements.reviewList.querySelectorAll('[data-review-editor]')].some(editor=>!editor.disabled)) {showToast('请先保存或取消当前编辑');return;}
          const back=state.reviewPageBack || [],action=button.dataset.reviewPage;
          if(action==='next' && state.reviewNextCursor)void loadReviews({pageCursor:state.reviewNextCursor,pageBack:[...back,state.reviewPageCursor || null]});
          else if(action==='previous' && back.length)void loadReviews({pageCursor:back[back.length-1],pageBack:back.slice(0,-1)});
          else if(action==='first')void loadReviews({pageCursor:null,pageBack:[]});
        }));
        container.querySelectorAll('[data-review-next]').forEach(button=>button.addEventListener('click',foregroundAction('', ()=>showConstructionFlow(state.constructionFlow,'step-publication'))));
        container.querySelectorAll('[data-review-dependency]').forEach(button=>button.addEventListener('click',()=> {
          if ([...elements.reviewList.querySelectorAll('[data-review-editor]')].some(editor=>!editor.disabled)) { showToast('请先保存或取消当前编辑'); return; }
          const target=state.reviews.find(item=>item.record_id === button.dataset.reviewDependency);
          if (!target) { showToast('关联实体状态可能已变化，请刷新审核队列'); return; }
          state.reviewPhase=target.trust?.status === 'QUARANTINED' ? 'paused' : 'identities'; renderReviews();
          $(`review-record-${target.record_id}`)?.scrollIntoView({behavior:'smooth',block:'center'});
        }));
        container.querySelectorAll('[data-review-assess]').forEach(button=>button.addEventListener('click',foregroundAction('', ()=>queueAssessment(state.reviews[Number(button.dataset.reviewAssess)],true))));
        container.querySelectorAll('[data-review-keep]').forEach(button=>button.addEventListener('click',foregroundAction('', ()=>keepExistingFact(Number(button.dataset.reviewKeep)))));
        bindResolutionActions(container);
      }
      function updateReviewAvailability(item) {
        const index=state.reviews.findIndex(value=>value.record_id === item.record_id && value.revision === item.revision);
        if (index<0) return;
        const check=reviewApproval(item);
        const select=elements.reviewList.querySelector(`[data-review-select="${index}"]`);
        if (select) select.title=check.note;
        const note=elements.reviewList.querySelector(`[data-review-note="${index}"]`);
        if(note) note.textContent=check.note;
        elements.reviewList.querySelectorAll(`[data-review-index="${index}"]`).forEach(button=> {
          if(button.dataset.reviewAction === 'APPROVED') {const buttonCheck=reviewApproval(item,button.dataset.factIndependent==='true');button.disabled=Boolean(state.reviewBusy) || reviewIsEditing(index) || !buttonCheck.allowed; button.title=buttonCheck.note;
            button.textContent=reviewConfirmLabel(item,button.dataset.factIndependent==='true');}
        });
      }
      function renderAssessment(item) {
        const index=state.reviews.findIndex(value=>value.record_id === item.record_id && value.revision === item.revision);
        const panel=elements.reviewList.querySelector(`[data-assessment-panel="${index}"]`);
        if (panel) {panel.innerHTML=assessmentMarkup(item,index);bindReviewActions(panel);}
        updateReviewAvailability(item);
        setReviewBusy(Boolean(state.reviewBusy));
      }
      function invalidateAssessments() {
        reviewModel(); state.reviewAssessments.clear();
        for(const job of state.assessmentQueue.splice(0)) job.finish();
      }
      function currentAssessmentJob(job) {
        return job.identityEpoch===state.identityEpoch && job.reviewEpoch===state.reviewEpoch && state.reviewAssessments.get(job.item.record_id)===job.entry && state.reviews.some(item=>item.record_id===job.item.record_id && item.revision===job.item.revision);
      }
      function pumpAssessments() {
        reviewModel();
        while(state.assessmentActive<2 && state.assessmentQueue.length) {
          const job=state.assessmentQueue.shift();
          if(!currentAssessmentJob(job)) {job.finish();continue;}
          if(job.entry.automatic && !reviewRowVisible(job.entry.row)) {
            state.reviewAssessments.delete(job.item.record_id);renderAssessment(job.item);job.finish();continue;
          }
          state.assessmentActive+=1; job.entry.status='loading'; renderAssessment(job.item);
          void (async()=> {
            try {
              const result=await apiRequest(`/v1/knowledge/review-assessments/${encodeURIComponent(job.item.record_id)}?expected_revision=${job.item.revision}`);
              if(!currentAssessmentJob(job)) return;
              if(result.record_id!==job.item.record_id || result.revision!==job.item.revision) throw new Error('记录版本已变化，请刷新审核队列');
              Object.assign(job.entry,result);
              if(!job.item.object_entity && !propertyAssignment(job.item)
                && (!job.entry.automatic || reviewRowVisible(job.entry.row)))
                await loadPropertyAssignment(job.item);
            } catch(error) {if(currentAssessmentJob(job)) Object.assign(job.entry,{status:'error',error:error.message});}
            finally {if(currentAssessmentJob(job)) renderAssessment(job.item);state.assessmentActive-=1;job.finish();pumpAssessments();}
          })();
        }
      }
      function queueAssessment(item, refresh=false, {automatic=false,row=null}={}) {
        reviewModel(); if(!item || item.record_kind==='ENTITY_MENTION') return Promise.resolve();
        const cached=reviewAssessment(item);
        if(cached && !refresh) {
          if(!automatic) cached.automatic=false;
          else if(cached.automatic) cached.row=row;
          return cached.done;
        }
        let finish;const done=new Promise(resolve=>{finish=resolve;});
        const entry={revision:item.revision,identityEpoch:state.identityEpoch,reviewEpoch:state.reviewEpoch,status:'queued',done,automatic,row};
        state.reviewAssessments.set(item.record_id,entry);
        state.assessmentQueue.push({item,entry,identityEpoch:state.identityEpoch,reviewEpoch:state.reviewEpoch,finish});
        renderAssessment(item);pumpAssessments();return done;
      }
      function setReviewBusy(busy, recordIds=null) {
        if(recordIds) state.reviewBusyRecords=new Set(recordIds);
        if(!busy) state.reviewBusyRecords=null;
        elements.reviewList.setAttribute?.('aria-busy',String(Boolean(busy || state.reviewLoading)));
        elements.reviewList.querySelectorAll('[data-review-record]').forEach(row=>{
          const pending=busy && state.reviewBusyRecords?.has(row.dataset.reviewRecord);
          row.classList?.toggle('review-saving',Boolean(pending));
          row.querySelector('[data-review-operation]')?.remove();

        });
        state.reviewBusy=busy;updatePublicationBusy();busy=Boolean(busy || state.publicationBusy);
        elements.reviewList.querySelectorAll('[data-assignment-toggle], [data-assignment-search], [data-assignment-query], [data-assignment-notes], [data-assignment-select], [data-assignment-save]').forEach(control=>{
          const index=Number(control.dataset.assignmentToggle ?? control.dataset.assignmentSearch ?? control.dataset.assignmentQuery ?? control.dataset.assignmentNotes ?? control.dataset.assignmentSelect ?? control.dataset.assignmentSave);
          const item=state.reviews[index],entry=item && propertyAssignment(item);
          control.disabled=busy || Boolean(entry?.loading) || (control.dataset.assignmentSave!==undefined && Boolean(entry?.error || !entry?.items?.some(target=>target.selectable && target.entity.entity_id===entry.selected && entry.selected!==item.subject.entity_id)));
        });
        updateReviewBulkActions();
        elements.reviewList.querySelectorAll('[data-review-action], [data-review-keep], [data-resolution-apply], [data-review-save-draft]').forEach(button=> {
          const index=Number(button.dataset.reviewIndex ?? button.dataset.reviewKeep ?? button.dataset.resolutionApply ?? button.dataset.reviewSaveDraft);
          const item=state.reviews[index];
          const editing=item && reviewIsEditing(index);
          button.disabled=busy || button.dataset.resolutionBlocked==='true' || Boolean(editing && button.dataset.reviewSaveDraft === undefined);
          if (!busy && !editing && item && button.dataset.reviewAction==='APPROVED') button.disabled=!reviewApproval(item,button.dataset.factIndependent==='true').allowed;
          if (!busy && !editing && item && button.dataset.reviewAction==='QUARANTINED') button.disabled=item.trust?.status==='QUARANTINED';
          if(button.getAttribute?.('aria-busy')==='true') button.disabled=true;
        });
      }
      function updateReviewBulkActions() {
        const controls=$('review-bulk-actions');
        if (!controls) return;
        const selectable=[...elements.reviewList.querySelectorAll('[data-review-select]')];
        controls.hidden=!selectable.length || Boolean(state.reviewLoading);
        const disabled=Boolean(state.reviewBusy || state.publicationBusy || state.reviewLoading) || !selectable.some(input=>input.checked && !input.disabled);
        controls.querySelectorAll('button').forEach(button=>button.disabled=disabled || button.getAttribute?.('aria-busy')==='true');
      }
      async function keepExistingFact(index) {
        if(state.reviewBusy || state.publicationBusy) {showToast('审核或发布正在保存，请稍候');return;}
        if(reviewIsEditing(index)) {showToast('请先保存或取消当前编辑，再处理重复事实');return;}
        const item=state.reviews[index];const result=item && reviewAssessment(item);
        if(!item || result?.status!=='DUPLICATE' || !result.matches?.length) {showToast('比对结果已变化，请重新检查');return;}
        const identityEpoch=state.identityEpoch;
        setReviewBusy(true,[item.record_id]);
        try {
          await apiRequest('/v1/knowledge/reviews:batch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({decisions:[{record_kind:'ASSERTION',record_id:item.record_id,expected_revision:item.revision,decision:'REJECTED',duplicate_of_revision_id:result.matches[0].record.revision_id,notes:'与当前权威事实语义一致；保留已有事实，不重复入图。本次原文与审核记录保留。'}]})});
          if(identityEpoch!==state.identityEpoch)return;
          showToast('已保留权威事实；本次来源和处理原因已留存');
          await loadReviews({anchorRecordId:item.record_id});
        } catch(error) {if(identityEpoch===state.identityEpoch)showToast(error.message);}
        finally {if(identityEpoch===state.identityEpoch) setReviewBusy(false);}
      }

      async function loadRevisionHistory(index, button) {
        const item = state.reviews[index];
        if (!item) { showToast('审核队列已变化，请刷新'); return; }
        const identityEpoch = state.identityEpoch;
        button.disabled = true;
        try {
          const payload = await apiRequest(`/v1/knowledge/records/${encodeURIComponent(item.record_id)}/revisions?limit=100`);
          if (identityEpoch !== state.identityEpoch) return;
          state.revisionHistories.set(item.record_id, {headRevision: item.revision, items: payload.items || []});
          renderReviewHistory(item,index);
        } catch (error) {
          if (identityEpoch !== state.identityEpoch) return;
          state.revisionHistories.set(item.record_id, {headRevision: item.revision, error: error.message});
          renderReviewHistory(item,index);
        }
      }

      function renderReviewHistory(item,index) {
        index=state.reviews.findIndex(value=>value.record_id===item.record_id && value.revision===item.revision);
        if (index<0) return;
        const panel=elements.reviewList.querySelector(`[data-review-history-panel="${index}"]`);
        if(panel) {panel.innerHTML=revisionHistoryMarkup(item,index);bindReviewActions(panel);}
      }

      function bindResolutionActions(container) {
        bindEvidenceActions(container);
        container.querySelectorAll('[data-resolution-search]').forEach(button=>button.addEventListener('click',()=> {
          const index=Number(button.dataset.resolutionSearch), item=state.reviews[index];
          const input=container.querySelector(`[data-resolution-query="${index}"]`);
          if(item) void queueResolution(item,{refresh:true,query:input?.value.trim() || ''});
        }));
        container.querySelectorAll('[data-resolution-load]').forEach(button => {
          button.addEventListener('click', foregroundAction('', () => loadResolution(Number(button.dataset.resolutionLoad), button)));
        });
        container.querySelectorAll('[data-resolution-apply]').forEach(button => {
          button.addEventListener('click', foregroundAction('', () => applyResolution(Number(button.dataset.resolutionApply), Number(button.dataset.resolutionSuggestion), button, button.dataset.resolutionManual === undefined ? null : Number(button.dataset.resolutionManual))));
        });
      }

      function renderResolution(item) {
        const index = state.reviews.findIndex(value => value.record_id === item.record_id && value.revision === item.revision);
        if (index < 0) return;
        const panel = elements.reviewList.querySelector(`[data-resolution-panel="${index}"]`);
        if (!panel) return;
        // Update only the suggestion panel: selections, edits and focus stay intact.
        const open=panel.querySelector?.('[data-resolution-details]')?.open;
        panel.innerHTML = resolutionMarkup(item, index);
        const details=panel.querySelector?.('[data-resolution-details]'); if(details) details.open=Boolean(open);
        bindResolutionActions(panel);
        updateReviewAvailability(item);
        setReviewBusy(Boolean(state.reviewBusy));
      }

      function invalidateReviewResolutions() {
        disconnectReviewDetails();
        invalidateAssessments();
        state.reviewPhase='identities';
        state.reviewEpoch += 1;
        state.reviews = [];
        state.resolutions.clear();
        for (const job of state.resolutionQueue.splice(0)) job.finish();
        // Active requests retain their slots until settled, even across identities.
      }

      function refreshReviewResolutions(identityEpoch = state.identityEpoch) {
        if (identityEpoch !== state.identityEpoch) return;
        invalidateAssessments();
        state.resolutions.clear();
        for (const job of state.resolutionQueue.splice(0)) job.finish();
        for(const item of state.reviews) {
          if(item.record_kind==='ENTITY_MENTION') renderResolution(item);
          else renderAssessment(item);
        }
        observeReviewDetails();
      }

      function currentResolutionJob(job) {
        return job.identityEpoch === state.identityEpoch && job.reviewEpoch === state.reviewEpoch
          && state.resolutions.get(job.item.record_id) === job.entry
          && state.reviews.some(item => item.record_id === job.item.record_id && item.revision === job.item.revision);
      }

      function pumpResolutions() {
        while (state.resolutionActive < 2 && state.resolutionQueue.length) {
          const job = state.resolutionQueue.shift();
          if (!currentResolutionJob(job)) { job.finish(); continue; }
          if(job.entry.automatic && !reviewRowVisible(job.entry.row)) {
            state.resolutions.delete(job.item.record_id);renderResolution(job.item);job.finish();continue;
          }
          state.resolutionActive += 1;
          job.entry.status = 'loading';
          renderResolution(job.item);
          void executeResolution(job);
        }
      }

      async function executeResolution(job) {
        const {item, entry} = job;
        try {
          const payload = await apiRequest(`/v1/knowledge/entity-resolution/${encodeURIComponent(item.record_id)}?expected_revision=${encodeURIComponent(item.revision)}${entry.query ? `&query=${encodeURIComponent(entry.query)}` : ''}`);
          if (!currentResolutionJob(job)) return;
          if (payload.revision !== item.revision || payload.record_id !== item.record_id) throw new Error('候选版本已变化，请刷新审核队列');
          Object.assign(entry, payload, {status: 'ready'});
        } catch (error) {
          if (!currentResolutionJob(job)) return;
          entry.status = 'error';
          entry.error = error.message;
        } finally {
          if (currentResolutionJob(job)) renderResolution(item);
          state.resolutionActive -= 1;
          job.finish();
          pumpResolutions();
        }
      }

      function queueResolution(item, {refresh = false, query = "", automatic=false, row=null} = {}) {
        const cached = state.resolutions.get(item.record_id);
        if (!refresh && cached?.revision === item.revision && cached.identityEpoch === state.identityEpoch
          && cached.reviewEpoch === state.reviewEpoch) {
          if(!automatic) cached.automatic=false;
          else if(cached.automatic) cached.row=row;
          return cached.done || Promise.resolve();
        }
        let finish;
        const done = new Promise(resolve => { finish = resolve; });
        const entry = {revision: item.revision, identityEpoch: state.identityEpoch,
          reviewEpoch: state.reviewEpoch, status: 'queued', done, query, automatic, row};
        state.resolutions.set(item.record_id, entry);
        state.resolutionQueue.push({item, entry, identityEpoch: state.identityEpoch,
          reviewEpoch: state.reviewEpoch, finish});
        renderResolution(item);
        pumpResolutions();
        return done;
      }

      async function loadResolution(index, button) {
        const item = state.reviews[index];
        if (!item || item.record_kind !== 'ENTITY_MENTION') { showToast('审核队列已变化，请刷新'); return; }
        if (button) button.disabled = true;
        await queueResolution(item, {refresh: true});
      }

      function identityResumeRequest(items) {
        const summary=state.latestAutoReview;
        if(!summary?.job_id || summary.status==='RUNNING')return null;
        const ids=items.filter(item=>item.record_kind==='ENTITY_MENTION'
          && (summary.items?.some(value=>value.record_id===item.record_id)
            || state.constructionReceipt?.job_id===summary.job_id
              && item.evidence?.version_id && item.evidence.version_id===state.constructionReceipt.version_id))
          .map(item=>item.record_id);
        return ids.length?{jobId:summary.job_id,ids:[...new Set(ids)]}:null;
      }

      async function applyResolution(index, suggestionIndex, button, manualIndex=null) {
        if(state.reviewBusy || state.publicationBusy) {showToast('审核或发布正在保存，请稍候');return;}
        if(reviewIsEditing(index)) {showToast('请先保存或取消当前编辑，再确认实体链接');return;}
        const item = state.reviews[index];
        const resolution = item && state.resolutions.get(item.record_id);
        const manual=manualIndex===null ? null : resolution?.review_targets?.[manualIndex];
        if(manual && !manual.selectable) {showToast(manual.reason);return;}
        const suggestion = manual ? {target:manual.entity} : resolution?.suggestions?.[suggestionIndex];
        if (!item || resolution?.revision !== item.revision || resolution.identityEpoch !== state.identityEpoch
          || resolution.reviewEpoch !== state.reviewEpoch || resolution.status !== 'ready' || !suggestion?.target) { showToast('消歧建议已变化，请重新匹配'); return; }
        // The user explicitly confirms the suggestion. Reuse a selectable target from
        // this same ready response so the server can validate its exact revision.
        const selectedTarget=manual || resolution.review_targets?.find(target=>target.selectable && target.entity.entity_id===suggestion.target.entity_id);
        const reviewNotes = globalThis.prompt('请输入本次实体链接的人工审核依据：', '已核对双方原文上下文，确认指向同一实体。');
        if (reviewNotes === null) return;
        if (!reviewNotes.trim()) { showToast('审核依据不能为空'); return; }
        if (!globalThis.confirm(`确认将“${item.entity?.canonical_name || item.record_id}”链接到已有实体“${suggestion.target.canonical_name}”？依赖事实只重绑，不会自动批准。`)) return;
        const identityEpoch = state.identityEpoch;
        let resume=null;
        button.disabled = true;
        setReviewBusy(true,[item.record_id]);
        try {
          const payload = await apiRequest('/v1/knowledge/entity-resolution:apply', {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({
              record_id: item.record_id,
              expected_revision: item.revision,
              target_entity_id: suggestion.target.entity_id,
              notes: reviewNotes.trim(),
              ...(selectedTarget ? {target_record_id:selectedTarget.record_id,target_expected_revision:selectedTarget.revision} : {}),
            }),
          });
          if (identityEpoch !== state.identityEpoch) return;
          const outcomes = payload.outcomes || [];
          resume=identityResumeRequest([item]);
          trackReviewedOutcomes(outcomes);
          invalidatePublicationPreview();
          state.resolutions.delete(item.record_id);
          showToast(`实体已链接；${Math.max(0, outcomes.length - 1)} 条依赖事实已重绑并保留待审核状态`);
          await loadReviews({anchorRecordId:item.record_id,impact:reviewRefreshImpact(outcomes,suggestion.target)});
          if(identityEpoch===state.identityEpoch) void Promise.allSettled([loadActiveDocuments(), loadPublicationCandidates()]);
        } catch (error) {
          if (identityEpoch === state.identityEpoch) showToast(error.message);
          button.disabled = false;
        } finally {
          if(identityEpoch===state.identityEpoch) {
            setReviewBusy(false);
            if(resume && state.latestAutoReview?.job_id===resume.jobId)await retryAutoReview(resume.jobId,resume.ids);
          }
        }
      }

      function reviewRefreshImpact(outcomes, targetEntity=null) {
        const recordIds=new Set(outcomes.map(item=>item.record_id)), entityIds=new Set(), entityTypes=new Set();
        for(const item of state.reviews.filter(item=>recordIds.has(item.record_id))) {
          for(const entity of [reviewEntity(item),item.object_entity].filter(Boolean)) if(entity.entity_id) entityIds.add(entity.entity_id);
          if(item.record_kind==='ENTITY_MENTION' && item.entity?.entity_type) entityTypes.add(item.entity.entity_type);
        }
        if(targetEntity?.entity_id) entityIds.add(targetEntity.entity_id);
        if(targetEntity?.entity_type) entityTypes.add(targetEntity.entity_type);
        return {recordIds,entityIds,entityTypes};
      }
      function reviewCheckUnaffected(item, impact, assignment=false) {
        if(!impact || impact.recordIds.has(item.record_id)) return false;
        const entities=[reviewEntity(item),item.object_entity].filter(Boolean);
        if(entities.some(entity=>impact.entityIds.has(entity.entity_id))) return false;
        // A new confirmed identity changes the available matching/assignment targets of its type.
        return !(item.record_kind==='ENTITY_MENTION' || assignment) || !entities.some(entity=>impact.entityTypes.has(entity.entity_type));
      }
      async function loadReviews({refreshResolutions = false, candidatesOnly = state.reviewCandidatesOnly || false, anchorRecordId = null, impact = null,refreshAutoReview=true, pageCursor = state.reviewPageCursor || null, pageBack = state.reviewPageBack || []} = {}) {
        const finish=state.reviewBusy || state.publicationBusy ? () => {} : beginButtonFeedback($('review-refresh-button'));
        try {
        reviewModel();
        const previousAssessments=new Map(state.reviewAssessments), previousAssignments=new Map(state.propertyAssignments);
        const previousRecords = new Set(state.reviews.map(item=>item.record_id));
        disconnectReviewDetails();
        invalidateAssessments();
        const identityEpoch = state.identityEpoch;
        const reviewEpoch = ++state.reviewEpoch;
        state.reviewLoading=true;updateReviewBulkActions();
        for (const job of state.resolutionQueue.splice(0)) job.finish();
        try {
          const queueUrl=(candidatesOnly ? '/v1/knowledge/review-queue?status=CANDIDATE&limit=100' : '/v1/knowledge/review-queue?status=CANDIDATE&status=QUARANTINED&limit=100')+(pageCursor?'&cursor='+encodeURIComponent(pageCursor):'');
          const payload = await apiRequest(queueUrl, {feedback:state.reviewBusy ? '正在更新审核队列…' : false});
          if (identityEpoch !== state.identityEpoch || reviewEpoch !== state.reviewEpoch) return;
          const pageChanged=pageCursor!==(state.reviewPageCursor || null);
          const anchor=pageChanged?null:captureReviewViewport(anchorRecordId);
          state.reviewPageCursor=pageCursor;state.reviewPageBack=pageBack;
          state.reviewCandidatesOnly=candidatesOnly;state.reviewNextCursor=payload.next_cursor || null;
          state.reviews = stableReviewOrder(state.reviews,Array.isArray(payload.items) ? payload.items : []);
          if (state.reviews.some(item=>item.record_kind==='ENTITY_MENTION' && item.trust?.status!=='QUARANTINED' && !previousRecords.has(item.record_id))) state.reviewPhase='identities';
          const current = new Map(state.reviews.map(item => [item.record_id, item.revision]));
          const itemsById=new Map(state.reviews.map(item=>[item.record_id,item]));
          // Reuse only completed checks on unchanged, unrelated records. Requests from the
          // previous epoch still fail their current-job checks and cannot overwrite these.
          for(const [recordId,entry] of previousAssessments) {
            const item=itemsById.get(recordId);
            if(item && entry.identityEpoch===identityEpoch && entry.revision===item.revision
              && !['queued','loading','error'].includes(entry.status) && reviewCheckUnaffected(item,impact)) {
              entry.reviewEpoch=reviewEpoch;state.reviewAssessments.set(recordId,entry);
            }
          }
          state.propertyAssignments=new Map();
          for(const [recordId,entry] of previousAssignments) {
            const item=itemsById.get(recordId);
            if(item && entry.identityEpoch===identityEpoch && entry.revision===item.revision && !entry.loading && !entry.error
              && reviewCheckUnaffected(item,impact,true)) {entry.reviewEpoch=reviewEpoch;state.propertyAssignments.set(recordId,entry);}
          }
          for (const [recordId, entry] of state.resolutions) {
            if (refreshResolutions || (impact && !reviewCheckUnaffected(itemsById.get(recordId) || {},impact)) || entry.identityEpoch !== identityEpoch || current.get(recordId) !== entry.revision
              || ['queued', 'loading'].includes(entry.status)) state.resolutions.delete(recordId);
            else entry.reviewEpoch = reviewEpoch;
          }
          state.reviewLoading=false;renderReviews({anchor});
          if(pageChanged)elements.reviewList.scrollIntoView({block:"start"});
          if(refreshAutoReview && state.latestAutoReview?.job_id && !state.autoReviewBusy && !state.constructionBusy) {
            try {await refreshConstructionReceipt(state.latestAutoReview.job_id,{renderReceipt:state.constructionReceipt?.job_id===state.latestAutoReview.job_id});}
            catch(error){if(identityEpoch===state.identityEpoch)showToast(`审核队列已刷新；构建摘要暂未更新：${error.message}`);}
          }
        } catch (error) {
          if (identityEpoch !== state.identityEpoch || reviewEpoch !== state.reviewEpoch) return;
          invalidateReviewResolutions();
          elements.reviewList.innerHTML = `<div class="output-box">${escapeHtml(error.message)}</div>`;
          state.reviewLoading=false;updateReviewBulkActions();
        }
        } finally {finish();}
      }

      function chosenReviews() {
        return [...elements.reviewList.querySelectorAll('[data-review-select]:checked')].map(input => Number(input.dataset.reviewSelect));
      }

      function independentReviewPreview(items, grouped = false) {
        if(grouped && (items.length < 2 || new Set(items.map(item=>item.entity?.entity_type)).size !== 1)) throw new Error('请选择至少两条类型一致的实体提及，核对原文后再共同建档');
        const properties=new Map(), facts=new Map();
        const sources=items.map((item,position)=> {
          const resolution=state.resolutions.get(item.record_id);
          for(const property of resolution?.identity_properties || []) {
            const values=properties.get(property.name) || new Set();
            values.add(JSON.stringify([property.datatype,property.canonical_value,property.canonical_unit || null]));
            properties.set(property.name,values);
          }
          for(const fact of resolution?.dependent_facts || []) facts.set(fact.record_id,fact);
          const quote=[...(item.evidence?.quoted_text || '请先展开文档原文核对')];
          return `${position+1}. ${item.entity?.canonical_name || '未命名实体'}\n原文摘录：${quote.slice(0,180).join('')}${quote.length>180 ? '…' : ''}`;
        });
        if(grouped && [...properties.values()].some(values=>values.size>1)) throw new Error('所选提及的身份依据存在矛盾，请分别确认或暂缓核查');
        return `${grouped ? '建立 1 个独立实体，归入全部所选提及' : `分别建立 ${items.length} 个独立实体`}。\n只处理以下 ${items.length} 条提及，同组其他提及保持待确认。\n\n${sources.join('\n\n')}\n\n将更新 ${facts.size} 条关联属性或关系的归属，仍需单独审核：${[...facts.values()].map(fact=>reviewPropertyLabel(fact.predicate)).join('、') || '无'}。\n本次仅保存审核决定，发布前仍可返回修改。`;
      }

      async function submitReviews(decision, indexes, saveEdits = false, groupIdentity = false, independentFact = false) {
        if(state.reviewBusy || state.publicationBusy) {showToast('审核或发布正在保存，请稍候');return;}
        const identityEpoch=state.identityEpoch, reviewEpoch=state.reviewEpoch;
        let resume=null;
        if (!indexes.length) { showToast('请先勾选审核记录'); return; }
        setReviewBusy(true, indexes.map(index=>state.reviews[index]?.record_id).filter(Boolean));
        try {
          const decisions = indexes.map(index => {
            const item = state.reviews[index];
            if (!item) throw new Error('审核队列已变化，请刷新');
            if(propertyAssignment(item)?.open) throw new Error('请先保存或取消归属更改，再审核此记录');
            if (decision==='APPROVED') {
              const check=reviewApproval(item,independentFact);
              if (!check.allowed) throw new Error(check.note);
            }
            const request = {
              record_kind: item.record_kind,
              record_id: item.record_id,
              expected_revision: item.revision,
              decision,
              notes: `Local Playground human review: ${decision}`,
            };
            const editor = elements.reviewList.querySelector(`[data-review-editor="${index}"]`);
            const editing = editor && !editor.disabled;
            if (editing && !saveEdits) throw new Error('请先保存或取消所选记录的编辑，再进行批量审核');
            if (saveEdits) {
              if (!editing || decision !== 'QUARANTINED' || indexes.length !== 1) throw new Error('修改后必须先保存并暂缓复核，重新检查后再批准');
              const edit = standardizedEdit(item, parseJsonEditor(editor, '审核编辑'));
              request[item.record_kind === 'ENTITY_MENTION' ? 'mention_edit' : 'assertion_edit'] = edit;
            }
            return request;
          });
          if(independentFact) {
            if(decisions.length!==1 || decision!=='APPROVED' || decisions[0].record_kind!=='ASSERTION' || saveEdits || groupIdentity)
              throw new Error('独立事实需要逐条确认，编辑内容请先保存');
            const item=state.reviews[indexes[0]];
            const reason=globalThis.prompt(`作为独立事实确认：${reviewFactText(item)}\n请说明为什么应独立保留，例如不同时间、工况、事件或对象。\n该理由作为人工审核记录保存，不作为原文事实；发布后不会与相同内容自动合并。（最多 2000 字）`,item.fact_distinction?.reason||'');
            if(reason===null)return;
            if(!reason.trim() || reason.trim().length>2000)throw new Error('请填写 1–2000 字的独立确认理由');
            decisions[0].fact_action='INDEPENDENT';decisions[0].fact_reason=reason.trim();
            decisions[0].notes='人工确认独立事实：'+reason.trim();
          }
          const identityRequests=decision==='APPROVED' ? decisions.filter(item=>item.record_kind==='ENTITY_MENTION') : [];
          if(groupIdentity && identityRequests.length !== decisions.length) throw new Error('共同建档只能选择实体提及');
          if(identityRequests.length) {
            const items=identityRequests.map(request=>state.reviews.find(item=>item.record_id===request.record_id));
            const preview=independentReviewPreview(items,groupIdentity);
            const reason=globalThis.prompt('请根据原文填写身份判断依据：为什么独立建档，或为什么所选提及属于同一对象？（最多 2000 字）','');
            if(reason===null) return;
            if(!reason.trim() || [...reason.trim()].length>2000) throw new Error('请填写 1–2000 字的身份判断依据');
            if(!globalThis.confirm(`${preview}\n\n审核依据：${reason.trim()}\n\n确认保存？`)) return;
            for(const request of identityRequests) {
              request.identity_action='INDEPENDENT'; request.notes=reason.trim();
              if(groupIdentity) request.identity_group='selected-mentions';
              const token=state.resolutions.get(request.record_id)?.impact_token;
              if(token) request.expected_identity_impact=token;
            }
          }
          if(identityEpoch!==state.identityEpoch || reviewEpoch!==state.reviewEpoch) throw new Error('审核上下文已变化，请刷新后重试');
          const payload = await apiRequest('/v1/knowledge/reviews:batch', {
            method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({decisions}),
          });
          if(identityEpoch!==state.identityEpoch) return;
          if(identityRequests.length)resume=identityResumeRequest(identityRequests.map(request=>
            state.reviews.find(item=>item.record_id===request.record_id)).filter(Boolean));
          trackReviewedOutcomes(payload.outcomes || []);
          invalidatePublicationPreview();
          if(saveEdits) state.reviewPhase='paused';
          showToast(saveEdits ? '修改已保存到暂缓区；新版本重新检查后，再确认采用' : `${payload.outcomes?.length || 0} 条审核决定已保存`);
          await loadReviews({anchorRecordId:decisions[0].record_id,impact:reviewRefreshImpact(payload.outcomes || [])});
          if(identityEpoch===state.identityEpoch) void Promise.allSettled([loadActiveDocuments(), loadPublicationCandidates()]);
        } catch (error) {
          if(identityEpoch===state.identityEpoch)showToast(error.message);
        } finally {
          if(identityEpoch===state.identityEpoch) {
            setReviewBusy(false);
            if(resume && state.latestAutoReview?.job_id===resume.jobId)await retryAutoReview(resume.jobId,resume.ids);
          }
        }
      }

      function activePublication() {
        return state.publications.find(item => item.status === 'ACTIVE') || null;
      }

      function publicationSelectedIds() {
        return new Set([...elements.publicationRevisions.value.split(/\s+/).filter(Boolean),...state.selectedCandidateRevisions]);
      }
      function writePublicationSelection(ids) {
        elements.publicationRevisions.value=[...ids].sort().join('\n');
        state.selectedCandidateRevisions=new Set(ids);
        for(const id of state.selectedPublicationCandidates.keys()) {
          if(!ids.has(id)) state.selectedPublicationCandidates.delete(id);
        }
        for(const candidate of state.publicationCandidates) {
          if(ids.has(candidate.record?.revision_id)) state.selectedPublicationCandidates.set(candidate.record.revision_id,candidate);
        }
        invalidatePublicationPreview();
      }
      function changePublicationSelection(ids, checked) {
        if(state.reviewBusy || state.publicationBusy || state.publicationCandidatesLoading || state.publicationBulkBusy) return;
        const selected=publicationSelectedIds();
        for(const id of ids) checked ? selected.add(id) : selected.delete(id);
        writePublicationSelection(selected);updatePublicationSelection();
      }
      function syncPublicationTextSelection() {
        const ids=new Set(elements.publicationRevisions.value.split(/\s+/).filter(Boolean));
        writePublicationSelection(ids);updatePublicationSelection();
      }
      function trackReviewedOutcomes(outcomes) {
        // A previous background list must not prune revisions approved by a newer action.
        state.publicationCandidatesEpoch=(state.publicationCandidatesEpoch || 0)+1;
        const selected=publicationSelectedIds();
        for(const outcome of outcomes) {
          selected.delete(outcome.previous_revision_id);
          state.approvedRevisions.delete(outcome.previous_revision_id);
          if(outcome.status==='APPROVED') {
            selected.add(outcome.revision_id);state.approvedRevisions.add(outcome.revision_id);
          }
        }
        writePublicationSelection(selected);
      }
      function publicationCandidateGroups() {
        const groups=new Map();
        state.publicationCandidates.forEach((candidate,index)=> {
          const item=candidate.record, key=reviewGroupKey(item);
          if(!groups.has(key)) groups.set(key,{key,entity:reviewEntity(item),rows:[]});
          groups.get(key).rows.push({candidate,item,index});
        });
        return [...groups.values()];
      }
      function updatePublicationSelection() {
        const selected=publicationSelectedIds(), groups=publicationCandidateGroups();
        elements.publicationCandidateList.querySelectorAll('[data-publication-selection-count]').forEach(label=>label.textContent=`跨页已选 ${selected.size} 条记录`);
        elements.publicationCandidateList.querySelectorAll('[data-publication-clear]').forEach(button=>button.disabled=!selected.size || state.reviewBusy || state.publicationBusy || state.publicationCandidatesLoading || state.publicationBulkBusy);
        elements.publicationCandidateList.querySelectorAll('[data-publication-candidate]').forEach(input=> {
          input.checked=selected.has(state.publicationCandidates[Number(input.dataset.publicationCandidate)]?.record?.revision_id);
        });
        elements.publicationCandidateList.querySelectorAll('[data-publication-group]').forEach(input=> {
          const rows=groups[Number(input.dataset.publicationGroup)].rows;
          const count=rows.filter(row=>selected.has(row.item.revision_id)).length;
          input.checked=count===rows.length;input.indeterminate=count>0 && count<rows.length;
        });
        elements.publicationCandidateList.querySelectorAll('[data-publication-selected-count]').forEach(label=> {
          const rows=groups[Number(label.dataset.publicationSelectedCount)].rows;
          label.textContent=`本组已选 ${rows.filter(row=>selected.has(row.item.revision_id)).length}/${rows.length} 条记录`;
        });
      }
      function publicationSourceMarkup(row, position, selected) {
        const {candidate,item,index}=row, mention=item.record_kind==='ENTITY_MENTION';
        const title=mention ? `来源提及 ${position+1}` : reviewFactText(item);
        const detail=mention ? '来源记录与版本' : `${standardizedTitle(item)}与版本`;
        return `<div class="review-match" data-publication-record="${escapeHtml(item.record_id)}" data-publication-entity-id="${escapeHtml(reviewEntity(item).entity_id||'')}" data-publication-predicate="${escapeHtml(item.predicate||'')}" tabindex="-1"><p class="publication-problem-note" data-publication-problem-note hidden></p><label class="checkbox-label"><input type="checkbox" data-publication-candidate="${index}" ${selected.has(item.revision_id)?'checked':''}> <strong>${escapeHtml(title)}</strong></label>${provenanceBadges(item.trust)}<span class="trust-badge">${escapeHtml(item.trust?.status==='APPROVED'?'已确认':'已发布，可恢复')}</span>${candidate.requires_replacement?'<p>本次将替换这条来源记录的已发布版本。</p>':''}${reviewEvidence(item.evidence,item.trust?.origin==='HUMAN_SUPPLEMENT'?'查看人工补充记录':'查看文档原文',item)}<details><summary>${detail}</summary><p>记录 ID：${escapeHtml(item.record_id)}<br>当前记录版本：${escapeHtml(item.revision)}<br>版本 ID：${escapeHtml(item.revision_id)}</p><pre>${escapeHtml(JSON.stringify(standardizedReview(item),null,2))}</pre></details>${item.trust?.status==='APPROVED'?`<button class="button" type="button" data-publication-reopen="${index}">${mention?'返回修改此来源':'返回修改此事实'}</button>`:''}</div>`;
      }
      function updatePublicationBusy() {
        const busy=Boolean(state.reviewBusy || state.publicationBusy || state.publicationCandidatesLoading || state.publicationBulkBusy);
        elements.publicationCandidateList?.querySelectorAll('input, [data-publication-reopen]').forEach(control=>control.disabled=busy);
        elements.publicationCandidateList?.querySelectorAll('[data-publication-page]').forEach(control=>control.disabled=busy || control.dataset.pageDisabled==='true');
        elements.publicationCandidateList?.querySelectorAll('[data-publication-select-page], [data-publication-select-all]').forEach(control=>control.disabled=busy || !state.publicationCandidates.length);
        elements.publicationCandidateList?.querySelectorAll('[data-publication-clear]').forEach(control=>control.disabled=busy || !publicationSelectedIds().size);
        elements.publicationRevisions.disabled=busy;elements.publicationRemovals.disabled=busy;
        if(typeof document!=='undefined') {
          const hasSelection=Boolean(elements.publicationRevisions.value.trim() || elements.publicationRemovals.value.trim() || state.selectedCandidateRevisions.size);
          const actions=$('publication-submit-actions');if(actions) actions.hidden=!state.publicationCandidates.length && !hasSelection && !state.publicationPreview;
          const preview=$('publication-preview-button');if(preview) preview.disabled=busy || !hasSelection || preview.getAttribute?.('aria-busy')==='true';
          const publish=$('publication-button');if(publish) publish.disabled=busy || !state.publicationPreview || publish.getAttribute?.('aria-busy')==='true';
        }
      }
      function renderPublicationCandidates({loadError=false} = {}) {
        if (!state.publicationCandidates.length) {
          elements.publicationCandidateList.innerHTML = `<div class="build-empty-state"><h3>${loadError?'尚未读取到候选列表。':'当前没有可发布的候选。'}</h3><p>${loadError?'本次读取未成功，无法判断是否存在可发布记录。请点击上方“刷新可发布候选”重试。':'资料完成复核后，会在这里列出待发布或可恢复的已确认记录。'}</p><div class="workbench-actions"><button class="button primary" type="button" data-publication-upload>去上传资料</button><button class="button" type="button" data-publication-review>查看复核队列</button></div></div>`;
          elements.publicationCandidateList.querySelectorAll('[data-publication-upload]').forEach(button=>button.addEventListener('click',foregroundAction('', ()=>showConstructionFlow(state.constructionFlow,'source-upload-slot'))));
          elements.publicationCandidateList.querySelectorAll('[data-publication-review]').forEach(button=>button.addEventListener('click',foregroundAction('', ()=>showConstructionFlow(state.constructionFlow,'step-review'))));
          if(publicationSelectedIds().size) {
            elements.publicationCandidateList.insertAdjacentHTML('beforeend',publicationPaginationMarkup());
            bindPublicationPagination();
          }
          updatePublicationBusy();return;
        }
        const selected=publicationSelectedIds(), groups=publicationCandidateGroups();
        const cards=groups.map((group,groupIndex)=> {
          const mentions=group.rows.filter(row=>row.item.record_kind==='ENTITY_MENTION');
          const properties=group.rows.filter(row=>row.item.record_kind!=='ENTITY_MENTION' && !row.item.object_entity);
          const relationships=group.rows.filter(row=>Boolean(row.item.object_entity));
          const count=group.rows.filter(row=>selected.has(row.item.revision_id)).length;
          const sections=[['来源提及',mentions],['属性',properties],['关系',relationships]].map(([label,rows])=>rows.length ? `<details><summary>${label}（${rows.length} 条记录）</summary>${rows.map((row,position)=>publicationSourceMarkup(row,position,selected)).join('')}</details>`:'').join('');
          return `<article class="review-entity-card" data-publication-entity="${escapeHtml(group.key)}" data-publication-entity-id="${escapeHtml(group.entity.entity_id||'')}" tabindex="-1"><p class="publication-problem-note" data-publication-problem-note hidden></p><header><input type="checkbox" data-publication-group="${groupIndex}" aria-label="选择${escapeHtml(group.entity.canonical_name)}的本页全部记录" ${count===group.rows.length?'checked':''}><h3>${escapeHtml(group.entity.canonical_name || '未命名实体')}</h3><span class="trust-badge">${escapeHtml(group.entity.entity_type)}</span><span>来源提及 ${mentions.length} · 属性 ${properties.length} · 关系 ${relationships.length}</span></header><div class="review-mention"><p>实体 ID：${escapeHtml(group.entity.entity_id || '尚未确定')} · <span data-publication-selected-count="${groupIndex}">本组已选 ${count}/${group.rows.length} 条记录</span></p>${!mentions.length?'<p>本页仅包含此实体的事实变更；发布预览会校验并带入所需的实体身份。</p>':''}${sections}</div></article>`;
        }).join('');
        elements.publicationCandidateList.innerHTML=`<p>全部候选共 ${state.publicationCandidateEntityTotal} 个实体、${state.publicationCandidateTotal} 条记录。本页涉及 ${groups.length} 个实体、${state.publicationCandidates.length} 条记录。</p><p>每页 100 条记录，同一实体的记录可能分布在多页。翻页保留选择；发布预览包含所有页面的已选内容。</p>${publicationPaginationMarkup()}${cards}${publicationPaginationMarkup()}`;
        bindPublicationPagination();
        elements.publicationCandidateList.querySelectorAll('[data-publication-reopen]').forEach(button=>button.addEventListener('click',foregroundAction('', ()=>reopenPublicationCandidate(Number(button.dataset.publicationReopen)))));
        elements.publicationCandidateList.querySelectorAll('[data-publication-candidate]').forEach(input=>input.addEventListener('change',()=> {
          const item=state.publicationCandidates[Number(input.dataset.publicationCandidate)]?.record;
          if(item) changePublicationSelection([item.revision_id],input.checked);
        }));
        elements.publicationCandidateList.querySelectorAll('[data-publication-group]').forEach(input=> {
          const rows=groups[Number(input.dataset.publicationGroup)].rows;
          const count=rows.filter(row=>selected.has(row.item.revision_id)).length;
          input.indeterminate=count>0 && count<rows.length;
          input.addEventListener('change',()=>changePublicationSelection(rows.map(row=>row.item.revision_id),input.checked));
        });
        bindEvidenceActions(elements.publicationCandidateList);updatePublicationBusy();applyPublicationIssue();
      }

      function publicationPaginationMarkup() {
        const offset=state.publicationCandidateOffset, total=state.publicationCandidateTotal;
        const pages=Math.max(1,Math.ceil(total/100)), page=Math.floor(offset/100)+1;
        return `<div class="workbench-actions" style="justify-content:flex-start" role="group" aria-label="发布候选分页"><button class="button primary" type="button" data-publication-select-all>选择全部候选</button><button class="button" type="button" data-publication-page="${offset-100}" data-page-disabled="${offset===0}" ${offset===0?'disabled':''}>上一页</button><span>第 ${page} / ${pages} 页</span><button class="button" type="button" data-publication-page="${offset+100}" data-page-disabled="${offset+100>=total}" ${offset+100>=total?'disabled':''}>下一页</button><button class="button" type="button" data-publication-select-page>选择本页全部记录</button><button class="button" type="button" data-publication-clear>清空跨页选择</button><span data-publication-selection-count aria-live="polite">跨页已选 ${publicationSelectedIds().size} 条记录</span></div>`;
      }

      async function selectAllPublicationCandidates() {
        if(state.reviewBusy || state.publicationBusy || state.publicationCandidatesLoading || state.publicationBulkBusy)return;
        const identity=state.identityEpoch;
        state.publicationBulkBusy=true;updatePublicationBusy();
        try {
          const payload=await apiRequest('/v1/knowledge/publication-candidates?limit=1&select_all=true');
          if(identity!==state.identityEpoch)return;
          if(!payload.selection_complete)throw new Error(payload.total>payload.selection_limit
            ? `候选数量超过当前整批处理上限 ${payload.selection_limit} 条，请缩小范围后重试。`
            : '候选内容已变化，请刷新后重新选择全部候选。');
          const rows=payload.selection || [], ids=new Set(rows.map(item=>item.revision_id));
          if(ids.size!==payload.total || rows.length!==payload.total)throw new Error('未能完整读取全部候选，原有选择已保留，请重试。');
          state.selectedPublicationCandidates=new Map(rows.map(item=>[item.revision_id,{record:{revision_id:item.revision_id,record_id:item.record_id},requires_replacement:item.requires_replacement}]));
          writePublicationSelection(ids);
          updatePublicationSelection();
          showToast(`已选择全部 ${ids.size} 条候选，请生成完整发布预览。`);
        } catch(error) {
          if(identity===state.identityEpoch)showToast(error.message);
        } finally {
          if(identity===state.identityEpoch){state.publicationBulkBusy=false;updatePublicationBusy();}
        }
      }

      function bindPublicationPagination() {
        elements.publicationCandidateList.querySelectorAll('[data-publication-select-all]').forEach(button=>button.addEventListener('click',foregroundAction('',selectAllPublicationCandidates)));
        elements.publicationCandidateList.querySelectorAll('[data-publication-page]').forEach(button=>button.addEventListener('click',foregroundAction('',async()=> {
          await loadPublicationCandidates(Number(button.dataset.publicationPage));
          elements.publicationCandidateList.scrollIntoView({block:'start'});
        })));
        elements.publicationCandidateList.querySelectorAll('[data-publication-select-page]').forEach(button=>button.addEventListener('click',()=>changePublicationSelection(state.publicationCandidates.map(item=>item.record.revision_id),true)));
        elements.publicationCandidateList.querySelectorAll('[data-publication-clear]').forEach(button=>button.addEventListener('click',()=> {
          if(state.reviewBusy || state.publicationBusy || state.publicationCandidatesLoading || state.publicationBulkBusy)return;
          writePublicationSelection(new Set());updatePublicationSelection();
        }));
      }

      async function loadPublicationCandidates(requestedOffset = state.publicationCandidateOffset) {
        // Event handlers may pass an Event; only explicit page numbers change pages.
        const offset=Number.isInteger(requestedOffset)?Math.max(0,requestedOffset):state.publicationCandidateOffset;
        const finish=state.reviewBusy || state.publicationBusy ? () => {} : beginButtonFeedback($('publication-candidates-refresh-button'));
        const identityEpoch = state.identityEpoch;
        const epoch=state.publicationCandidatesEpoch=(state.publicationCandidatesEpoch || 0)+1;
        state.publicationCandidatesRequestEpoch=epoch;
        state.publicationCandidatesLoading=true;updatePublicationBusy();
        try {
          let payload = await apiRequest(`/v1/knowledge/publication-candidates?limit=100&offset=${offset}`);
          if (identityEpoch !== state.identityEpoch || epoch!==state.publicationCandidatesEpoch) return;
          // Publishing/removal can leave the current page beyond the new end.
          if(offset>0 && offset>=payload.total) {
            const lastOffset=Math.max(0,Math.ceil(payload.total/100)-1)*100;
            payload=await apiRequest(`/v1/knowledge/publication-candidates?limit=100&offset=${lastOffset}`);
          }
          if (identityEpoch !== state.identityEpoch || epoch!==state.publicationCandidatesEpoch) return;
          // Paging does not alter the selection or invalidate its existing preview.
          if(offset===state.publicationCandidateOffset) invalidatePublicationPreview();
          state.publicationCandidates = Array.isArray(payload.items) ? payload.items : [];
          state.publicationCandidateOffset=payload.offset;
          state.publicationCandidateTotal=payload.total;
          state.publicationCandidateEntityTotal=payload.total_entities;
          const selected=publicationSelectedIds();
          for(const candidate of state.publicationCandidates) {
            if(selected.has(candidate.record.revision_id)) state.selectedPublicationCandidates.set(candidate.record.revision_id,candidate);
          }
          renderPublicationCandidates();
          if(state.reviews.length===0 && !state.reviewLoading)renderReviews();
        } catch (error) {
          if (identityEpoch !== state.identityEpoch || epoch!==state.publicationCandidatesEpoch) return;
          invalidatePublicationPreview();
          renderPublicationCandidates({loadError:true});
          elements.publicationCandidateList.insertAdjacentHTML('afterbegin',`<div class="output-box" role="alert">分页读取失败，已保留选择。${escapeHtml(error.message)}</div>`);
        } finally {
          if(identityEpoch===state.identityEpoch && epoch===state.publicationCandidatesRequestEpoch) {
            state.publicationCandidatesLoading=false;updatePublicationBusy();
          }
          finish();
        }
      }

      function renderHistory() {
        if(state.maintenanceActions){state.maintenanceActions.history(state.publications,elements.historyList);return;}
        if (!state.publications.length) {
          elements.historyList.innerHTML = '<div class="output-box">当前工作区尚未发布知识。</div>';
          return;
        }
        elements.historyList.innerHTML = ''+state.publications.map(item => `<article class="governance-item"><div class="governance-item-head"><div><strong>第 ${escapeHtml(item.generation)} 版</strong><p>${escapeHtml(item.published_revision_ids?.length || 0)} 条知识记录 · ${escapeHtml(item.created_at)}</p></div><span class="trust-badge ${item.status === 'ACTIVE' ? 'authoritative' : ''}">${item.status === 'ACTIVE' ? '当前生效' : '历史版本 · 当前未生效'}</span></div></article>`).join('');
      }

      async function loadHistory() {
        const finish=state.reviewBusy || state.publicationBusy ? () => {} : beginButtonFeedback($('history-refresh-button'));
        try {
        const identityEpoch = state.identityEpoch;
        const requestEpoch=state.historyRequestEpoch=(state.historyRequestEpoch||0)+1;
        try {
          const payload = await apiRequest('/v1/knowledge/publications?limit=100');
          if (identityEpoch !== state.identityEpoch || requestEpoch!==state.historyRequestEpoch) return;
          state.publications = Array.isArray(payload.items) ? payload.items : [];
          renderHistory();
        } catch (error) {
          if (identityEpoch !== state.identityEpoch || requestEpoch!==state.historyRequestEpoch) return;
          elements.historyList.innerHTML = `<div class="output-box">${escapeHtml(error.message)}</div>`;
        }
        } finally {finish();}
      }

      function inventoryLiteralMarkup(literal) {
        if (!literal) return '<span>缺少受治理字面量</span>';
        const displayValue = literal.canonical_value ?? literal.typed_value ?? literal.value;
        const details = [
          literal.datatype ? `datatype ${literal.datatype}` : null,
          literal.canonical_unit ? `unit ${literal.canonical_unit}` : null,
          literal.valid_from ? `valid from ${literal.valid_from}` : null,
          literal.valid_to ? `valid to ${literal.valid_to}` : null,
          literal.observed_at ? `observed ${literal.observed_at}` : null,
        ].filter(Boolean).map(escapeHtml).join(' · ');
        return `<span>${escapeHtml(displayValue)}${details ? `<br>${details}` : ''}</span>`;
      }

      function invalidateInventory() {
        state.inventoryEpoch += 1;
        $('inventory-export-status').textContent = "";
        state.activeInventory = null;
        state.selectedInventoryRevisions.clear();
        state.inventoryRevisionHistories.clear();
        state.inventoryHistoryRequests.clear();
        const removals = elements.publicationRemovals.value.split(/\s+/).filter(Boolean);
        elements.publicationRemovals.value = removals.filter(value => !state.inventoryRemovalRecordIds.has(value)).join('\n');
        if (removals.length !== elements.publicationRemovals.value.split(/\s+/).filter(Boolean).length) invalidatePublicationPreview();
        state.inventoryRemovalRecordIds.clear();
        renderInventory();
        return state.inventoryEpoch;
      }

      function inventoryEntityMarkup(entity) {
        if (!entity) return '<span>缺少受治理实体</span>';
        return `<strong>${escapeHtml(entity.display_name)}</strong><br>${escapeHtml(entity.entity_type)} · ${escapeHtml(entity.canonical_key)}<br>entity ${escapeHtml(entity.entity_id)}`;
      }

      function inventoryEvidenceMarkup(evidence, label = 'record evidence') {
        if (!evidence) return `<div class="exact-evidence">${escapeHtml(label)} · 不可用</div>`;
        const document = evidence.document_id ? `Document ${escapeHtml(evidence.document_id)} · version ${escapeHtml(evidence.version_id)}<br>` : '';
        return `<div class="exact-evidence"><strong>${escapeHtml(label)}</strong><br>${document}Chunk ${escapeHtml(evidence.chunk_id)} · ordinal ${escapeHtml(evidence.ordinal)} · chars ${escapeHtml(evidence.char_start)}:${escapeHtml(evidence.char_end)}<br>仅显示证据位置；源文本未由清单接口返回。</div>`;
      }

      function inventoryRelationshipPropertiesMarkup(assertion) {
        const values = Array.isArray(assertion?.relationship_properties) ? assertion.relationship_properties : [];
        if (!values.length) return '';
        return `<div class="provider-note" style="margin-top:8px"><strong>关系属性</strong></div>${values.map(value => `<div class="governance-item" style="margin-top:7px"><strong>${escapeHtml(value.name)}</strong><p>property value ${escapeHtml(value.property_value_id)} · confidence ${escapeHtml(value.confidence)}<br>${inventoryLiteralMarkup(value.literal)}</p>${inventoryEvidenceMarkup(value.evidence, 'property evidence')}</div>`).join('')}`;
      }

      function inventoryRevisionHistoryMarkup(item, index) {
        const history = state.inventoryRevisionHistories.get(item.record_id);
        if (!history || history.headRevisionId !== item.revision_id) return `<div class="workbench-actions"><button class="button" type="button" data-inventory-history="${index}">查看不可变 revision 历史</button></div>`;
        if (history.error) return `<div class="output-box">${escapeHtml(history.error)}</div><div class="workbench-actions"><button class="button" type="button" data-inventory-history="${index}">重试 revision 历史</button></div>`;
        const rows = (history.items || []).map(revision => `<div class="exact-evidence"><strong>revision ${escapeHtml(revision.revision)} · ${escapeHtml(revision.trust?.status)}</strong> · ${escapeHtml(revision.revision_id)}<br>${escapeHtml(revision.trust?.authority)} · ${escapeHtml(revision.trust?.origin)} · ${escapeHtml(revision.trust?.reviewed_by || 'unreviewed')}${revision.trust?.reviewed_at ? ` · ${escapeHtml(revision.trust.reviewed_at)}` : ''}</div>`).join('');
        return `<div class="provider-note" style="margin-top:10px"><strong>不可变 revision 历史</strong><br>通过既有 ACL 安全接口读取；此处不投影证据正文或审核备注。</div>${rows || '<div class="output-box">没有可见 revision。</div>'}<div class="workbench-actions"><button class="button" type="button" data-inventory-history="${index}">刷新 revision 历史</button></div>`;
      }

      function renderInventory() {
        const inventory = state.activeInventory;
        if (!inventory) {
          elements.inventorySummary.textContent = '已发布知识尚未加载。';
          elements.inventoryList.innerHTML = '<div class="output-box">已发布知识尚未加载。</div>';
          return;
        }
        if(state.maintenanceActions){state.maintenanceActions.inventory(inventory,elements.inventoryList,elements.inventorySummary,state.selectedInventoryRevisions,(item,checked)=>{if(checked)state.selectedInventoryRevisions.add(item.revision_id);else state.selectedInventoryRevisions.delete(item.revision_id);});return;}
        const filter = inventory.document_id ? ` · Document filter ${inventory.document_id}` : '';
        elements.inventorySummary.textContent = `active publication generation ${inventory.publication_generation} · ${inventory.publication_id} · T-Box ${inventory.ontology_version_id} · manifest ${inventory.manifest_hash} · ${number(inventory.matching_record_count)} / ${number(inventory.total_record_count)} records${filter}${inventory.truncated ? ' · 返回列表已按 limit 截断' : ''}`;
        if (!Array.isArray(inventory.items) || !inventory.items.length) {
          elements.inventoryList.innerHTML = '<div class="output-box">当前筛选没有已发布记录；active publication 仍已通过完整清单校验。</div>';
          return;
        }
        elements.inventoryList.innerHTML = inventory.items.map((item, index) => {
          const entity = item.entity;
          const assertion = item.assertion;
          const object = assertion?.object_kind === 'entity'
            ? inventoryEntityMarkup(assertion.object_entity)
            : inventoryLiteralMarkup(assertion?.literal);
          const structure = entity
            ? `<div class="provider-note" style="margin-top:8px">${inventoryEntityMarkup(entity)}</div>`
            : `<div class="provider-note" style="margin-top:8px"><strong>subject</strong><br>${inventoryEntityMarkup(assertion?.subject)}<br><br><strong>predicate</strong><br>${escapeHtml(assertion?.predicate)}<br><br><strong>object · ${escapeHtml(assertion?.object_kind)}</strong><br>${object}</div>${inventoryRelationshipPropertiesMarkup(assertion)}`;
          return `<article class="governance-item"><div class="governance-item-head"><label class="checkbox-label"><input type="checkbox" data-inventory-select="${index}" ${state.selectedInventoryRevisions.has(item.revision_id) ? 'checked' : ''}> <strong>${escapeHtml(item.record_kind)} · ${escapeHtml(entity?.display_name || assertion?.predicate || item.record_id)}</strong></label><div class="badge-row"><span class="trust-badge ${item.authority_level === 'AUTHORITATIVE' ? 'authoritative' : ''}">${escapeHtml(item.authority_level)}</span><span class="trust-badge">${escapeHtml(item.governance_status)}</span><span class="trust-badge">${escapeHtml(item.origin)}</span><span class="trust-badge">confidence ${escapeHtml(item.confidence)}</span></div></div><p>record ${escapeHtml(item.record_id)}<br>revision ${escapeHtml(item.revision_id)} · ontology ${escapeHtml(item.ontology_key)}</p>${structure}${inventoryEvidenceMarkup(item.evidence)}${inventoryRevisionHistoryMarkup(item, index)}</article>`;
        }).join('');
        elements.inventoryList.querySelectorAll('[data-inventory-select]').forEach(input => {
          input.addEventListener('change', () => {
            const item = state.activeInventory?.items?.[Number(input.dataset.inventorySelect)];
            if (!item) return;
            if (input.checked) state.selectedInventoryRevisions.add(item.revision_id);
            else state.selectedInventoryRevisions.delete(item.revision_id);
          });
        });
        elements.inventoryList.querySelectorAll('[data-inventory-history]').forEach(button => {
          button.addEventListener('click', foregroundAction('', () => loadInventoryRevisionHistory(Number(button.dataset.inventoryHistory), button)));
        });
      }

      async function loadInventory() {
        const finish=state.reviewBusy || state.publicationBusy ? () => {} : beginButtonFeedback($('inventory-refresh-button'));
        try {
        const identityEpoch = state.identityEpoch;
        const requestEpoch = invalidateInventory();
        const documentId = elements.inventoryDocumentFilter.value.trim();
        const limit = Number(elements.inventoryLimit.value);
        if (!elements.inventoryLimit.checkValidity() || !Number.isSafeInteger(limit)) {
          elements.inventorySummary.textContent = '知识清单参数无效。';
          elements.inventoryList.innerHTML = '<div class="output-box">返回记录上限必须是 1–500 的整数。</div>';
          return;
        }
        const params = new URLSearchParams({limit: String(limit)});
        if (documentId) params.set('document_id', documentId);
        elements.inventorySummary.textContent = '';
        elements.inventoryList.innerHTML = '';
        try {
          const payload = await apiRequest(`/v1/knowledge/publication-inventory?${params.toString()}`);
          if (identityEpoch !== state.identityEpoch || requestEpoch !== state.inventoryEpoch) return;
          state.activeInventory = payload;
          renderInventory();
        } catch (error) {
          if (identityEpoch !== state.identityEpoch || requestEpoch !== state.inventoryEpoch) return;
          state.activeInventory = null;
          state.selectedInventoryRevisions.clear();
          state.inventoryRevisionHistories.clear();
          const message = error.status === 403
            ? '当前身份缺少 knowledge:quality，或不能完整查看 active publication 的全部 ACL。'
            : error.status === 409
              ? '当前租户没有唯一且质量合格的 active publication，或 publication / T-Box / manifest 状态冲突。'
              : error.status === 503
                ? '活动 A-Box 清单依赖暂不可用；未返回或缓存部分图谱。'
                : error.message;
          elements.inventorySummary.textContent = '已发布知识清单暂不可用；可从质量检查定位问题。';
          elements.inventoryList.innerHTML = `<div class="output-box">${escapeHtml(message)}</div>`;
        }
        } finally {finish();}
      }

      async function exportInventory() {
        const identity = state.identityEpoch, epoch = state.inventoryEpoch;
        const documentId = elements.inventoryDocumentFilter.value.trim();
        const status = $('inventory-export-status');
        const isCurrent = () => identity === state.identityEpoch && epoch === state.inventoryEpoch
          && documentId === elements.inventoryDocumentFilter.value.trim();
        if (!elements.inventoryDocumentFilter.checkValidity()) {
          status.textContent = '来源编号无效，请检查后重试。';
          return;
        }
        const params = new URLSearchParams();
        if (documentId) params.set('document_id', documentId);
        const publicationId = state.activeInventory?.publication_id;
        if (publicationId) params.set('publication_id', publicationId);
        status.textContent = '正在校验并导出已发布实例…';
        try {
          const payload = await apiRequest(`/v1/knowledge/publication-inventory/export?${params}`);
          if (!isCurrent()) return;
          const file = instanceExportFile(payload, {documentId, publicationId});
          if (!payload.items.length) {
            status.textContent = '当前来源筛选下没有已发布实例，请调整筛选后重试。';
            return;
          }
          const url = URL.createObjectURL(new Blob([file.text], {type:'application/json;charset=utf-8'}));
          const link = document.createElement('a');
          link.href = url;
          link.download = file.name;
          document.body.append(link);
          try { link.click(); } finally {
            link.remove();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
          }
          status.textContent = `已导出第 ${payload.publication_generation} 版，共 ${number(payload.items.length)} 条记录${documentId ? '（当前来源）' : '（完整发布版本）'}。`;
        } catch (error) {
          if (!isCurrent()) return;
          status.textContent = error.status === 403
            ? '当前身份无权导出完整知识清单。'
            : error.status === 409
              ? '当前没有可导出的完整发布版本、版本已变化或超过导出上限，请刷新清单后重试。'
              : error.status === 503 || error.status === 504
                ? '导出依赖暂不可用，请稍后重试；未生成部分文件。'
                : error.message;
        } finally {
          if (identity === state.identityEpoch && epoch === state.inventoryEpoch && !isCurrent()) {
            status.textContent = '筛选已变化，请重新导出。';
          }
        }
      }

      async function loadInventoryRevisionHistory(index, button) {
        const item = state.activeInventory?.items?.[index];
        if (!item) { showToast('活动 A-Box 已变化，请刷新'); return; }
        const identityEpoch = state.identityEpoch;
        const requestEpoch = state.inventoryEpoch;
        const requestToken = Symbol();
        state.inventoryHistoryRequests.set(item.record_id, requestToken);
        const isCurrent = () => identityEpoch === state.identityEpoch && requestEpoch === state.inventoryEpoch && state.inventoryHistoryRequests.get(item.record_id) === requestToken;
        button.disabled = true;
        try {
          const payload = await apiRequest(`/v1/knowledge/records/${encodeURIComponent(item.record_id)}/revisions?limit=100`);
          if (!isCurrent()) return;
          state.inventoryRevisionHistories.set(item.record_id, {headRevisionId: item.revision_id, items: payload.items || []});
          renderInventory();
        } catch (error) {
          if (!isCurrent()) return;
          state.inventoryRevisionHistories.set(item.record_id, {headRevisionId: item.revision_id, error: error.status === 403 ? '当前身份缺少 knowledge:review，不能读取 revision 历史。' : error.message});
          renderInventory();
        }
      }

      function addInventoryRemovals(confirmed = false) {
        if(state.maintenanceActions && confirmed!==true){
          const inventory=state.activeInventory,epoch=state.inventoryEpoch,identity=state.identityEpoch;
          const selected=(inventory?.items||[]).filter(i=>state.selectedInventoryRevisions.has(i.revision_id));
          if(!selected.length){showToast('请先选择要移除的知识');return;}
          state.maintenanceActions.removals(selected,()=>{addInventoryRemovals(true);void previewKnowledgePublication();},()=>inventory===state.activeInventory && epoch===state.inventoryEpoch && identity===state.identityEpoch && selected.length===state.selectedInventoryRevisions.size && selected.every(i=>state.selectedInventoryRevisions.has(i.revision_id)));return;
        }
        const items = state.activeInventory?.items || [];
        const selectedRecordIds = items
          .filter(item => state.selectedInventoryRevisions.has(item.revision_id))
          .map(item => item.record_id);
        if (!selectedRecordIds.length) { showToast('请先勾选至少一个活动 revision'); return; }
        const existing = elements.publicationRemovals.value.split(/\s+/).map(value => value.trim()).filter(Boolean);
        selectedRecordIds.filter(value => !existing.includes(value)).forEach(value => state.inventoryRemovalRecordIds.add(value));
        const recordIds = [...new Set([...existing, ...selectedRecordIds])];
        elements.publicationRemovals.value = recordIds.join('\n');
        invalidatePublicationPreview();
        showConstructionFlow('business', 'step-publication');
        elements.publicationRemovals.focus();
        showToast(`已将 ${selectedRecordIds.length} 条知识加入待移除清单`);
      }

      function renderQuality() {
        const report = state.quality;
        if(report && state.qualityPresenter){state.qualityPresenter.render(report,elements.qualityContent);return;}
        elements.qualityContent.innerHTML = report ? qualityReportMarkup(report, 'active publication') : '<div class="output-box">活动图谱质量尚未加载。</div>';
      }

      function qualityReportMarkup(report, label) {
        const countLabels = {
          revisions: 'revisions', entity_mentions: 'mentions', assertions: 'assertions',
          relationship_assertions: 'relationships', literal_assertions: 'literals', canonical_entities: 'entities',
        };
        const counts = Object.entries(countLabels).map(([key, label]) => `<span class="graph-count">${escapeHtml(label)} ${escapeHtml(report.counts?.[key] ?? 0)}</span>`).join('');
        const issues = Array.isArray(report.issues) && report.issues.length
          ? report.issues.map(item => `<article class="governance-item"><div class="governance-item-head"><strong>${escapeHtml(item.code)}</strong><span class="trust-badge ${item.severity === 'ERROR' ? 'danger' : ''}">${escapeHtml(item.severity)}</span></div><p>${escapeHtml(item.object_kind)} · ${escapeHtml(item.object_id)}<br>${escapeHtml(item.detail)}<br>issue ${escapeHtml(item.issue_id)}</p></article>`).join('')
          : '<div class="output-box">未发现结构或一致性问题。</div>';
        const sample = Array.isArray(report.review_sample) && report.review_sample.length
          ? report.review_sample.map(item => `<article class="governance-item"><strong>${escapeHtml(item.object_kind)} · ${escapeHtml(item.object_id)}</strong><p>${escapeHtml((item.issue_codes || []).join(', '))}<br>evidence Chunks: ${escapeHtml((item.evidence_chunk_ids || []).join(', ') || '—')}</p></article>`).join('')
          : '<div class="output-box">当前没有人工复核样本。</div>';
        return `
          <article class="governance-item">
            <div class="governance-item-head"><strong>${escapeHtml(label)} · generation ${escapeHtml(report.publication_generation)}</strong><span class="trust-badge ${report.passed ? 'authoritative' : 'danger'}">${report.passed ? 'PASS' : 'FAIL'}</span></div>
            <p>${escapeHtml(report.publication_id)}<br>T-Box ${escapeHtml(report.ontology_version_id)} · checksum ${escapeHtml(report.tbox_checksum)}<br>graph digest ${escapeHtml(report.graph_digest)}<br>manifest ${escapeHtml(report.manifest_hash)} · corpus revision ${escapeHtml(report.corpus_revision)}<br>ruleset ${escapeHtml(report.ruleset_version)} · run ${escapeHtml(report.run_id)}</p>
            <div class="graph-summary">${counts}</div>
            <div class="exact-evidence">issues ${escapeHtml(report.total_issue_count)} · errors ${escapeHtml(report.total_error_count)}${report.issues_truncated ? ' · 有界列表已截断' : ''}</div>
          </article>
          <div><strong>问题</strong></div>${issues}
          <div><strong>确定性人工复核样本</strong></div>${sample}`;
      }

      async function loadQuality() {
        const finish=state.reviewBusy || state.publicationBusy ? () => {} : beginButtonFeedback($('quality-refresh-button'));
        try {
        const identityEpoch = state.identityEpoch;
        const requestEpoch = ++state.qualityEpoch;
        state.quality = null;
        state.qualityPresenter?.invalidate(elements.qualityContent);
        elements.qualityContent.innerHTML = '';
        try {
          const payload = await apiRequest('/v1/knowledge/quality');
          if (identityEpoch !== state.identityEpoch || requestEpoch !== state.qualityEpoch) return;
          state.quality = payload;
          renderQuality();
        } catch (error) {
          if (identityEpoch !== state.identityEpoch || requestEpoch !== state.qualityEpoch) return;
          state.quality = null;
          elements.qualityContent.innerHTML = `<div class="output-box">${escapeHtml(qualityErrorMessage(error))}</div>`;
        }
        } finally {finish();}
      }

      function qualityErrorMessage(error) {
        return error.status === 403
          ? '当前身份缺少 knowledge:quality，或无法完整查看该审计记录的全部 ACL。'
          : error.status === 404
            ? '未找到当前身份可见的审计记录；请刷新历史。'
            : error.status === 409
              ? '当前 publication / T-Box / 图谱版本已变化或记录一致性冲突；请刷新后重试。'
              : error.status === 503
                ? '质量审计依赖暂不可用；请稍后重试。'
                : error.message;
      }

      function renderQualityHistory() {
        elements.qualityHistoryList.innerHTML = state.qualityHistory.length
          ? state.qualityHistory.map((item, index) => `<article class="governance-item"><div class="governance-item-head"><strong>历史检查 · 第 ${escapeHtml(item.publication_generation)} 版</strong><span class="trust-badge ${item.passed ? 'authoritative' : 'danger'}">${item.passed ? '自动检查通过' : '发现阻断性错误'}</span></div><p>问题 ${escapeHtml(item.total_issue_count)} 项 · 错误 ${escapeHtml(item.total_error_count)} 项${item.issues_truncated ? ' · 仍有未显示的问题' : ''}<br>保存人 ${escapeHtml(item.recorded_by)} · ${escapeHtml(item.recorded_at)}</p><details><summary>版本与审计标识</summary><p>${escapeHtml(item.run_id)}<br>${escapeHtml(item.publication_id)}<br>${escapeHtml(item.ontology_version_id)}</p></details><div class="workbench-actions"><button class="button" type="button" data-quality-history="${index}">查看历史报告</button></div></article>`).join('')
          : '<div class="output-box">当前身份和筛选范围内没有已保存的质量审计。</div>';
        elements.qualityHistoryList.querySelectorAll('[data-quality-history]').forEach(button => {
          button.addEventListener('click', foregroundAction('', () => loadQualityRun(state.qualityHistory[Number(button.dataset.qualityHistory)]?.run_id)));
        });
      }

      async function loadQualityHistory() {
        const finish=state.reviewBusy || state.publicationBusy ? () => {} : beginButtonFeedback($('quality-history-refresh-button'));
        try {
        const identityEpoch = state.identityEpoch;
        const requestEpoch = ++state.qualityHistoryEpoch;
        state.qualityHistory = [];
        state.qualityDetailEpoch += 1;
        state.qualityPresenter?.invalidate(elements.qualityHistoryDetail);
        elements.qualityHistoryList.innerHTML = '';
        elements.qualityHistoryDetail.innerHTML = '<div class="output-box">选择历史记录查看当时的观察；历史结果不代表当前图谱状态。</div>';
        const params = new URLSearchParams({limit: '10'});
        const publicationId = elements.qualityHistoryPublication.value.trim();
        if (publicationId) params.set('publication_id', publicationId);
        try {
          const payload = await apiRequest(`/v1/knowledge/quality/runs?${params.toString()}`);
          if (identityEpoch !== state.identityEpoch || requestEpoch !== state.qualityHistoryEpoch) return;
          state.qualityHistory = payload.items || [];
          renderQualityHistory();
        } catch (error) {
          if (identityEpoch !== state.identityEpoch || requestEpoch !== state.qualityHistoryEpoch) return;
          elements.qualityHistoryList.innerHTML = `<div class="output-box">${escapeHtml(qualityErrorMessage(error))}</div>`;
        }
        } finally {finish();}
      }

      async function loadQualityRun(runId) {
        if (!runId) return;
        const identityEpoch = state.identityEpoch;
        const requestEpoch = ++state.qualityDetailEpoch;
        state.qualityPresenter?.invalidate(elements.qualityHistoryDetail);
        elements.qualityHistoryDetail.innerHTML = '';
        try {
          const payload = await apiRequest(`/v1/knowledge/quality/runs/${encodeURIComponent(runId)}`);
          if (identityEpoch !== state.identityEpoch || requestEpoch !== state.qualityDetailEpoch) return;
          if(state.qualityPresenter){state.qualityPresenter.render(payload.report,elements.qualityHistoryDetail,true);return;}
          elements.qualityHistoryDetail.innerHTML = `<div class="exact-evidence"><strong>历史观察，不代表当前图谱状态</strong><br>首次观察者 ${escapeHtml(payload.recorded_by)} · ${escapeHtml(payload.recorded_at)}<br>record hash ${escapeHtml(payload.record_hash)}</div>${qualityReportMarkup(payload.report, '历史观察（非实时）')}`;
        } catch (error) {
          if (identityEpoch !== state.identityEpoch || requestEpoch !== state.qualityDetailEpoch) return;
          elements.qualityHistoryDetail.innerHTML = `<div class="output-box">${escapeHtml(qualityErrorMessage(error))}</div>`;
        }
      }

      async function saveQualityRun() {
        if (state.qualitySaving) return;
        const identityEpoch = state.identityEpoch;
        const requestEpoch = ++state.qualitySaveEpoch;
        const isCurrent = () => identityEpoch === state.identityEpoch && requestEpoch === state.qualitySaveEpoch;
        state.qualitySaving = true;
        elements.qualitySaveButton.disabled = true;
        elements.qualitySaveOutput.textContent = '';
        try {
          const payload = await apiRequest('/v1/knowledge/quality/runs', {
            method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}',
          });
          if (!isCurrent()) return;
          elements.qualitySaveOutput.textContent = `已保存历史观察 ${payload.report.run_id} · ${payload.report.passed ? 'PASS' : 'FAIL'} · 首次观察者 ${payload.recorded_by} · ${payload.recorded_at}。重复 run 保留原首次记录；此结果不替换实时审计。`;
          await loadQualityHistory();
        } catch (error) {
          if (!isCurrent()) return;
          elements.qualitySaveOutput.textContent = qualityErrorMessage(error);
        } finally {
          if (isCurrent()) {
            state.qualitySaving = false;
            elements.qualitySaveButton.disabled = false;
          }
        }
      }

      const documentBlockerLabels = {
        ACTIVE_KNOWLEDGE_PUBLICATION: '当前生效知识仍引用此资料，请先在已发布知识维护中移除相关知识并发布',
        CURRENT_REVIEW: '仍有待处理的候选或审核记录，请先完成审核处置',
        ACTIVE_CONSTRUCTION_JOB: '知识构建任务仍在运行',
        ACTIVE_INGESTION_JOB: '文档写入任务仍在运行',
      };

      function renderActiveDocuments() {
        const visibleChunks = state.activeDocuments.reduce((total, item) => total + Number(item.chunk_count || 0), 0);
        elements.documentLifecycleSummary.textContent = `当前可维护来源：${number(state.activeDocuments.length)} 份资料 · ${number(visibleChunks)} 个片段（最多显示 100 份）`;
        if (!state.activeDocuments.length) {
          elements.documentLifecycleList.innerHTML = '<div class="output-box">当前身份没有完整可见的活动文档。</div>';
          return;
        }
        elements.documentLifecycleList.innerHTML = state.activeDocuments.map((item, index) => {
          const blockers = Array.isArray(item.blocker_codes) ? item.blocker_codes : [];
          const blockerMarkup = blockers.length
            ? `<div class="exact-evidence"><strong>撤回阻塞：</strong> ${blockers.map(code => `${escapeHtml(documentBlockerLabels[code] || '存在未识别的治理阻塞，请查看技术详情')}`).join('<br>')}</div>`
            : '<div class="exact-evidence"><strong>可撤回：</strong> 未发现生效知识引用、待处理审核记录或运行中任务。</div>';
          const action = item.blocked
            ? ''
            : `<div class="workbench-actions"><button class="button danger" type="button" data-retire-document="${index}">确认撤回此活动文档</button></div>`;
          return `<article class="governance-item" data-maintenance-document="${escapeHtml(item.document_id)}"><div class="governance-item-head"><div><strong>${escapeHtml(item.title)}</strong><p>${escapeHtml(item.source_name)} · ${escapeHtml(item.chunk_count)} 个来源片段</p></div><span class="trust-badge ${item.blocked ? 'danger' : 'authoritative'}">${item.blocked ? '需先处理依赖' : '可撤回'}</span></div>${blockerMarkup}${action}<details><summary>版本、访问范围与技术详情</summary><pre>${escapeHtml(JSON.stringify({document_id:item.document_id,source_uri:item.canonical_uri,version_id:item.active_version_id,snapshot_id:item.active_snapshot_id,access_groups:item.access_groups,blocker_codes:item.blocker_codes},null,2))}</pre></details></article>`;
        }).join('');
        elements.documentLifecycleList.querySelectorAll('[data-retire-document]').forEach(button => {
          button.addEventListener('click', foregroundAction('', () => retireActiveDocument(Number(button.dataset.retireDocument), button)));
        });
      }

      async function loadActiveDocuments() {
        const finish=state.reviewBusy || state.publicationBusy ? () => {} : beginButtonFeedback($('document-lifecycle-refresh-button'));
        try {
        const identityEpoch = state.identityEpoch;
        const requestEpoch=state.activeDocumentRequestEpoch=(state.activeDocumentRequestEpoch||0)+1;
        elements.documentLifecycleSummary.textContent = '';
        try {
          const payload = await apiRequest('/v1/knowledge/documents?limit=100');
          if (identityEpoch !== state.identityEpoch || requestEpoch!==state.activeDocumentRequestEpoch) return;
          state.activeDocuments = Array.isArray(payload.items) ? payload.items : [];
          renderActiveDocuments();
        } catch (error) {
          if (identityEpoch !== state.identityEpoch || requestEpoch!==state.activeDocumentRequestEpoch) return;
          state.activeDocuments = [];
          const message = error.status === 403
            ? '当前身份缺少 knowledge:lifecycle，或没有完整可见的活动文档。'
            : error.message;
          elements.documentLifecycleSummary.textContent = '实时授权视图不可用；未用启动 fixture 数字代替。';
          elements.documentLifecycleList.innerHTML = `<div class="output-box">${escapeHtml(message)}</div>`;
        }
        } finally {finish();}
      }

      function retirementStorageKey(item) {
        const tenant = currentPersona()?.tenant_id || 'unknown';
        const persona = currentPersona()?.id || 'unknown';
        return `graphrag-document-retirement-operation:${encodeURIComponent(tenant)}:${encodeURIComponent(persona)}:${encodeURIComponent(item.document_id)}`;
      }

      function nextDocumentRetirementOperation(item) {
        const fingerprint = JSON.stringify({
          tenant_id: currentPersona()?.tenant_id,
          persona_id: currentPersona()?.id,
          document_id: item.document_id,
          expected_active_snapshot_id: item.active_snapshot_id,
          source_generation: item.source_generation,
        });
        const storageKey = retirementStorageKey(item);
        let operation = null;
        try { operation = JSON.parse(sessionStorage.getItem(storageKey) || 'null'); } catch (_) {}
        if (!operation || operation.fingerprint !== fingerprint || !operation.operationKey) {
          const nonce = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
          operation = {fingerprint, operationKey: `playground-retire-${nonce}`};
          try { sessionStorage.setItem(storageKey, JSON.stringify(operation)); } catch (_) {}
        }
        return {storageKey, operationKey: operation.operationKey};
      }

      async function retireActiveDocument(index, button) {
        const item = state.activeDocuments[index];
        if (!item || item.blocked || (item.blocker_codes || []).length) {
          showToast('文档状态已变化或仍有治理阻塞，请刷新列表');
          return;
        }
        const confirmed = window.confirm(`确认撤回“${item.title}”？\n\n它将立即退出当前检索；原始资料、来源片段与审核记录仍会保留。`);
        if (!confirmed) return;
        const identityEpoch = state.identityEpoch;
        const operation = nextDocumentRetirementOperation(item);
        button.disabled = true;
        try {
          const payload = await apiRequest(`/v1/knowledge/documents/${encodeURIComponent(item.document_id)}:retire`, {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({
              operation_key: operation.operationKey,
              expected_active_snapshot_id: item.active_snapshot_id,
              source_generation: item.source_generation,
            }),
          });
          try { sessionStorage.removeItem(operation.storageKey); } catch (_) {}
          if (identityEpoch !== state.identityEpoch) return;
          showToast(`文档已受治理撤回：${shortId(payload.document_id)} · corpus revision ${payload.corpus_revision}`);
          state.knowledgeBrowser?.reset();
          resetRetrievalResult();
          await Promise.allSettled([loadActiveDocuments(), loadConstructionJobs(), loadReviews(), loadPublicationCandidates(), loadHistory(), loadInventory(), loadQuality(), loadQualityHistory()]);
        } catch (error) {
          if (identityEpoch !== state.identityEpoch) return;
          const message = error.status === 409
            ? '撤回被活动知识引用或并发版本变化阻止；请先完成审核 / publication 移除并刷新文档。'
            : error.message;
          showToast(message);
          button.disabled = false;
        }
      }

      async function reopenPublicationCandidate(index) {
        if(state.reviewBusy || state.publicationBusy) return;
        if([...elements.reviewList.querySelectorAll('[data-review-editor]')].some(editor=>!editor.disabled)) {
          showToast('请先保存或取消编辑与确认实例中的修改');return;
        }
        const item=state.publicationCandidates[index]?.record;
        if(!item || item.trust?.status!=='APPROVED') return;
        const identityEpoch=state.identityEpoch;
        setReviewBusy(true);invalidatePublicationPreview();
        state.publicationCandidatesEpoch=(state.publicationCandidatesEpoch || 0)+1;
        try {
          const payload=await apiRequest('/v1/knowledge/reviews:batch',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({decisions:[{record_kind:item.record_kind,record_id:item.record_id,
              expected_revision:item.revision,decision:'QUARANTINED',notes:'发布前返回修改，撤销本条审核确认。'}]})});
          if(identityEpoch!==state.identityEpoch) return;
          trackReviewedOutcomes(payload.outcomes || []);
          state.approvedRevisions.delete(item.revision_id);state.selectedCandidateRevisions.delete(item.revision_id);
          elements.publicationRevisions.value=elements.publicationRevisions.value.split(/\s+/).filter(id=>id!==item.revision_id).join('\n');
          await Promise.allSettled([loadReviews(),loadPublicationCandidates()]);
          if(identityEpoch!==state.identityEpoch) return;
          state.reviewPhase='paused';renderReviews();
          showConstructionFlow(state.constructionFlow,'step-review');
          $(`review-record-${item.record_id}`)?.scrollIntoView({behavior:'smooth',block:'center'});
        } catch(error) {if(identityEpoch===state.identityEpoch) showToast(error.message);}
        finally {if(identityEpoch===state.identityEpoch) setReviewBusy(false);}
      }

      async function correctPublishedRecord(recordId, expectedRevisionId = null) {
        if(state.reviewBusy || state.publicationBusy)throw new Error('正在审核或发布，请稍候。');
        if([...elements.reviewList.querySelectorAll('[data-review-editor]')].some(editor=>!editor.disabled))throw new Error('请先保存或取消正在编辑的内容。');
        const identity=state.identityEpoch;
        const read=()=>apiRequest(`/v1/knowledge/records/${encodeURIComponent(recordId)}/revisions?limit=1`);
        setReviewBusy(true);
        try{
          let item=(await read()).items?.[0];if(identity!==state.identityEpoch)return;
          if(!item)throw new Error('没有找到可访问的知识记录。');
          if(expectedRevisionId && item.revision_id!==expectedRevisionId && ['APPROVED','PUBLISHED'].includes(item.trust.status))throw new Error('已有更新的审核版本，请先刷新并核对修订历史。');
          if(['APPROVED','PUBLISHED'].includes(item.trust.status)){
            const payload=await apiRequest('/v1/knowledge/reviews:batch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({decisions:[{record_kind:item.record_kind,record_id:item.record_id,expected_revision:item.revision,decision:'QUARANTINED',notes:'从知识维护发起修正，等待重新审核与发布。'}]})});
            if(identity!==state.identityEpoch)return;state.knowledgeBrowser?.reset();
          trackReviewedOutcomes(payload.outcomes||[]);invalidatePublicationPreview();
          }else if(!['CANDIDATE','QUARANTINED'].includes(item.trust.status))throw new Error('该记录不能直接修正，请刷新审核队列。');
          await Promise.allSettled([loadReviews(),loadPublicationCandidates()]);if(identity!==state.identityEpoch)return;
          item=(await read()).items?.[0];if(identity!==state.identityEpoch)return;
          if(!item || !['CANDIDATE','QUARANTINED'].includes(item.trust.status))throw new Error('审核状态已变化，请重新定位。');
          const index=state.reviews.findIndex(r=>r.record_id===item.record_id);
          if(index<0)state.reviews.push(item);else state.reviews[index]=item;
          state.reviewPhase='paused';renderReviews();showConstructionFlow('business','step-review');
          $(`review-record-${item.record_id}`)?.scrollIntoView({behavior:'smooth',block:'center'});
          if(item.record_kind==='ENTITY_MENTION')void queueResolution(item);else void queueAssessment(item);
          showToast('已定位待修正记录；保存、审核并重新发布后生效。');
        }finally{if(identity===state.identityEpoch)setReviewBusy(false);}
      }

      function publicationSelection() {
        const revisionIds=[...new Set([...elements.publicationRevisions.value.split(/\s+/).filter(Boolean),...state.selectedCandidateRevisions])].sort();
        const removeRecordIds=[...new Set(elements.publicationRemovals.value.split(/\s+/).filter(Boolean))].sort();
        const replaceRecordIds=[...new Set([...state.selectedPublicationCandidates.values(),...state.publicationCandidates].filter(item=>revisionIds.includes(item.record?.revision_id) && item.requires_replacement).map(item=>item.record.record_id))].sort();
        if(!revisionIds.length && !removeRecordIds.length) throw new Error('请先选择待发布内容');
        if(removeRecordIds.some(id=>replaceRecordIds.includes(id))) throw new Error('同一记录不能同时移除和替换');
        return {approved_revision_ids:revisionIds, expected_active_publication_id:activePublication()?.publication_id || null,
          remove_record_ids: removeRecordIds, replace_record_ids: replaceRecordIds};
      }
      function clearPublicationIssue() {
        state.publicationIssue=null;
        elements.publicationCandidateList?.querySelectorAll('.publication-problem').forEach(node=>node.classList.remove('publication-problem'));
        elements.publicationCandidateList?.querySelectorAll('[data-publication-problem-note]').forEach(note=>{note.hidden=true;note.textContent='';});
      }
      function publicationProblemNodes(target) {
        const root=elements.publicationCandidateList;
        if(!root || !target)return [];
        const rows=[...root.querySelectorAll('[data-publication-record]')];
        if(target.record_id)return rows.filter(row=>row.dataset.publicationRecord===target.record_id);
        if(!target.entity_id)return [];
        const matches=rows.filter(row=>row.dataset.publicationEntityId===target.entity_id && (!target.predicate || row.dataset.publicationPredicate===target.predicate));
        return matches.length?matches:[...root.querySelectorAll('[data-publication-entity]')].filter(row=>row.dataset.publicationEntityId===target.entity_id);
      }
      function applyPublicationIssue() {
        const issue=state.publicationIssue;
        if(!issue)return;
        for(const target of issue.targets)for(const node of publicationProblemNodes(target)) {
          node.classList.add('publication-problem');
          const note=node.querySelector('[data-publication-problem-note]');
          if(note){note.hidden=false;note.textContent=[target.predicate?reviewPropertyLabel(target.predicate):'',issue.property_name?reviewPropertyLabel(issue.property_name):'',issue.message].filter(Boolean).join(' · ');}
          for(let parent=node.parentElement;parent && parent!==elements.publicationCandidateList;parent=parent.parentElement) {
            if(parent.tagName==='DETAILS')parent.open=true;
          }
        }
      }
      function showPublicationIssue(error) {
        clearPublicationIssue();
        const panel=$('publication-preview');
        if(!panel)return;
        const issue=error.publicationIssue;
        if(!issue) {
          panel.innerHTML=`<p role="alert">无法发布：${escapeHtml(error.message)}</p><p>本次未能定位到具体记录，请刷新候选后重新预览。${error.payload?.request_id?`如仍失败，请反馈问题编号：${escapeHtml(error.payload.request_id)}`:''}</p>`;
          return;
        }
        state.publicationIssue=issue;
        const hasVisibleLocation=issue.targets.some(target=>publicationProblemNodes(target).length>0);
        const locations=issue.targets.map((target,index)=> {
          const candidate=target.record_id?state.publicationCandidates.find(c=>c.record.record_id===target.record_id)?.record:null;
          const name=target.entity_name || (candidate?reviewEntity(candidate).canonical_name:'') || '相关实体';
          const detail=candidate?reviewFactText(candidate):target.predicate?reviewPropertyLabel(target.predicate):'实体资料';
          const found=publicationProblemNodes(target).length>0;
          return `<li><strong>${escapeHtml(name)} · ${escapeHtml(detail)}${issue.property_name?` · ${escapeHtml(reviewPropertyLabel(issue.property_name))}`:''}</strong> ${found?`<button class="button" type="button" data-publication-locate="${index}">定位问题</button>`:'<span>该记录不在当前候选列表，请在已有知识中核对或刷新候选。</span>'}</li>`;
        }).join('');
        const sourceHelp=issue.reason==='SOURCE_VERSION_CONFLICT'
          ? '<p>本次候选与将保留的已发布知识引用了同一文档的不同版本。不同资料若沿用同一个“稳定来源 URI”，会被识别为同一文档的更新。</p><p>若是独立业务资料，请回到上传步骤，重新选择文件并核对来源 URI，再构建、审核；本批误用来源的候选请返回复核后不采纳。若确为原文更新，请在知识维护中核对旧版记录的移除影响，统一版本后重新预览。无需清空知识库。</p><button class="button" type="button" data-publication-upload>返回上传资料</button>' : '';
        panel.innerHTML=`<p role="alert"><strong>无法发布：</strong>${escapeHtml(issue.message)}</p>${sourceHelp}${locations?`<ul>${locations}</ul>${hasVisibleLocation?'<p>已标记当前列表中的相关内容，可使用原有“返回修改”按钮处理后重新预览。</p>':''}`:''}${issue.truncated?'<p>这里只列出前 50 条相关记录，处理后请重新检查。</p>':''}`;
        panel.querySelector('[data-publication-upload]')?.addEventListener('click',foregroundAction('', ()=>showConstructionFlow('business','source-upload-slot')));
        applyPublicationIssue();
        panel.querySelectorAll('[data-publication-locate]').forEach(button=>button.onclick=()=> {
          const node=publicationProblemNodes(issue.targets[Number(button.dataset.publicationLocate)])[0];
          node?.scrollIntoView({behavior:'smooth',block:'center'});node?.focus({preventScroll:true});
        });
      }
      function invalidatePublicationPreview() {
        const hadPreview=Boolean(state.publicationPreview);
        clearPublicationIssue();
        state.publicationPreview=null;
        if (elements.publicationOutput) {
          elements.publicationOutput.textContent = '';
          elements.publicationOutput.hidden = true;
        }
        state.publicationPreviewEpoch=(state.publicationPreviewEpoch || 0)+1;
        const button=typeof document!=='undefined' && $('publication-button');
        if(button) button.disabled=true;
        const panel=typeof document!=='undefined' && $('publication-preview');
        if(panel) panel.innerHTML=hadPreview ? '<p>内容或选择已变化，请重新生成发布预览。</p>' : '';
        updatePublicationBusy();
      }
      function publicationPreviewMarkup(preview) {
        const sections=[['实体',preview.entity_changes],['属性',preview.property_changes],['关系',preview.relationship_fact_changes || preview.relationship_changes]];
        const labels={CREATE:'新增',UPDATE:'更新',REMOVE:'移除'};
        const summary=sections.map(([label,rows])=>`${label}：${['CREATE','UPDATE','REMOVE'].map(op=>`${labels[op]} ${rows.filter(row=>row.operation===op).length}`).join('，')}`).join(' · ');
        const scope=preview.source_scope;
        const sourceRows=items=>items.map(item=>`<li>${escapeHtml(item.title)}<details><summary>文档版本</summary><p>${escapeHtml(item.document_id)} · ${escapeHtml(item.version_id)}</p></details></li>`).join('') || '<li>无</li>';
        const sources=scope?`<h4>正式检索的文档范围</h4><p>发布后加入 ${scope.added.length} 个文档版本、退出 ${scope.removed.length} 个文档版本；保持 ${scope.unchanged_count} 个。</p><details ${scope.added.length?'open':''}><summary>加入检索</summary><ul>${sourceRows(scope.added)}</ul></details><details ${scope.removed.length?'open':''}><summary>退出检索</summary><ul>${sourceRows(scope.removed)}</ul></details>`:'';
        return `<p><strong>${summary}</strong></p><p>检查通过：审核状态、实体依赖、本体约束和精确引用有效。以下内容尚未发布。</p>${sources}`+sections.map(([label,rows])=>rows.map(change=>{
          const after=change.after || change.before;
          const title=after.standard_name || after.property_name || after.relationship_type || after.entity_id;
          return `<article class="publication-preview-card"><strong>${label} · ${escapeHtml(title)} · ${label==='关系' && change.operation==='UPDATE'?'更新来源':labels[change.operation]}</strong>${label==='关系'?`<p>新增来源 ${change.sources_added?.length||0} 条 · 移除来源 ${change.sources_removed?.length||0} 条${change.after?` · 发布后保留 ${change.after.source_count||1} 条来源`:''}</p>`:''}${provenanceBadges({authority:after.authority_level,origin:after.origin})}${after.fact_distinction?`<p>人工确认为独立事实，发布后独立保留。区分理由（审核判断）：${escapeHtml(after.fact_distinction.reason)}</p>`:''}<pre>${escapeHtml(JSON.stringify(change.after || change.before,null,2))}</pre>${change.before && change.after ? `<details><summary>更新前</summary><pre>${escapeHtml(JSON.stringify(change.before,null,2))}</pre></details>`:''}</article>`;
        }).join('')).join('')+`<details><summary>查看完整实例 JSON（${preview.instances_after.summary.entity_count} 个实体 · ${preview.instances_after.summary.property_count} 条属性 · ${preview.instances_after.summary.relationship_count} 条关系）</summary><p>待发布预览：包含本次发布后保留的完整实例及精确来源，并非数据库写入格式。</p><pre class="output-box">${escapeHtml(JSON.stringify(preview.instances_after,null,2))}</pre></details><div class="workbench-actions"><button class="button" type="button" data-publication-back>返回编辑和确认实例</button></div>`;
      }
      async function previewKnowledgePublication() {
        if(state.reviewBusy || state.publicationBusy) return;
        invalidatePublicationPreview();
        const identityEpoch=state.identityEpoch, epoch=state.publicationPreviewEpoch;
        const panel=$('publication-preview');
        try {
          const selection=publicationSelection(), selectionKey=JSON.stringify(selection);
          panel.innerHTML='';
          const preview=await apiRequest('/v1/knowledge/publications:preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(selection)});
          if(identityEpoch!==state.identityEpoch || epoch!==state.publicationPreviewEpoch || selectionKey!==JSON.stringify(publicationSelection())) return;
          state.publicationPreview={selection,selectionKey,preview};
          panel.innerHTML=publicationPreviewMarkup(preview);
          panel.querySelector('[data-publication-back]')?.addEventListener('click',foregroundAction('', ()=>showConstructionFlow(state.constructionFlow,'step-review')));
          $('publication-button').disabled=false;
        } catch(error) {if(identityEpoch===state.identityEpoch && epoch===state.publicationPreviewEpoch) showPublicationIssue(error);}
      }
      async function publishKnowledge() {
        if(state.reviewBusy || state.publicationBusy) return;
        const identityEpoch=state.identityEpoch;
        try {
          const selection=publicationSelection(), saved=state.publicationPreview;
          if(!saved || saved.selectionKey!==JSON.stringify(selection)) throw new Error('请先生成并查看当前选择的完整发布预览');
          state.publicationBusy=true;setReviewBusy(state.reviewBusy);
          invalidateInventory();
          const button=typeof document!=='undefined' && $('publication-button'); if(button) button.disabled=true;
          const payload=await apiRequest('/v1/knowledge/publications:publish',{
            method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({...saved.selection,expected_preview_hash:saved.preview.preview_hash}),
          });
          if(identityEpoch!==state.identityEpoch) return;
          invalidateInventory();invalidatePublicationPreview();refreshReviewResolutions(identityEpoch);
          state.approvedRevisions.clear();state.selectedCandidateRevisions.clear();state.selectedPublicationCandidates.clear();
          elements.publicationRevisions.value='';elements.publicationRemovals.value='';
          state.knowledgeBrowser?.reset();
          showToast(`知识 ${shortId(payload.publication_id)} 已发布`);
          await Promise.allSettled([loadPublicationCandidates(),loadHistory(),loadInventory(),loadQuality(),loadQualityHistory(),loadActiveDocuments()]);
          if(identityEpoch===state.identityEpoch) output(elements.publicationOutput,payload);
        } catch(error) {
          if(identityEpoch===state.identityEpoch) {invalidatePublicationPreview();showPublicationIssue(error);output(elements.publicationOutput,error.status===409 ? `发布状态已变化，请重新查看预览。${error.message}` : error.message);}
        } finally {if(identityEpoch===state.identityEpoch) {state.publicationBusy=false;setReviewBusy(state.reviewBusy);}}
      }

      async function rollbackPublication(index, expectedActive = null) {
        if(state.reviewBusy || state.publicationBusy) throw new Error('正在审核或发布，请稍候再切换版本。');
        const target = state.publications[index];
        const active = activePublication();
        if(expectedActive && active?.publication_id!==expectedActive)throw new Error('生效版本已变化，请重新比较。');
        if (!target || !active) { showToast('当前没有可用的生效版本，请刷新发布历史'); return; }
        const identityEpoch = state.identityEpoch;
        state.publicationBusy=true;setReviewBusy(state.reviewBusy);
        invalidateInventory();invalidatePublicationPreview();
        try {
          const payload = await apiRequest(`/v1/knowledge/publications/${encodeURIComponent(target.publication_id)}:rollback`, {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({expected_active_publication_id: active.publication_id}),
          });
          if (identityEpoch !== state.identityEpoch) return;
          refreshReviewResolutions(identityEpoch);
          state.knowledgeBrowser?.reset();
          showToast(`已切换至第 ${payload.generation} 版，文档与知识同步生效`);
          await Promise.allSettled([loadHistory(), loadPublicationCandidates(), loadInventory(), loadQuality(), loadQualityHistory(), loadActiveDocuments()]);
        } catch (error) {
          if (identityEpoch === state.identityEpoch) showToast(error.message);
          if(expectedActive)throw error;
        } finally {if(identityEpoch===state.identityEpoch) {state.publicationBusy=false;setReviewBusy(state.reviewBusy);}}
      }


      function reset() {

        invalidatePublicationPreview();
        state.ontologies = [];
        invalidateReviewResolutions();
        elements.reviewList.innerHTML = '<div class="output-box">审核队列尚未加载。</div>';
        state.reviewLoading=false;updateReviewBulkActions();
        state.revisionHistories.clear();
        state.constructionJobs = [];
        state.reviewPageCursor=null;state.reviewPageBack=[];state.reviewNextCursor=null;state.reviewCandidatesOnly=false;
        state.constructionDetailRequests?.clear();
        state.latestAutoReview=null;state.autoReviewBusy=false;state.constructionReceipt=null;state.autoReviewEpoch=(state.autoReviewEpoch || 0)+1;
        $('review-auto-summary').innerHTML='';$('review-auto-summary').hidden=true;
        $('construction-validation-summary').innerHTML = '';
        state.constructionOperation = null;
        resetUploadDraft();
        clearDemoSourceBinding();
        state.expertRevisionIds = [];
        state.expertImportMayReplace = false;
        state.lastConstructionMode = null;
        $('abox-publication-panel').hidden = true;
        $('construction-next').hidden = true;
        elements.aboxEditor.value = '';
        refreshABoxPreparation('身份已切换，已清除之前身份的实例草稿。请在当前身份下上传资料并选择“权威资料”，再准备专家实例。');
        state.publications = [];
        state.knowledgeBrowser?.reset();
        state.qualityPresenter?.reset();
        state.maintenanceActions?.reset();
        invalidateInventory();
        state.quality = null;
        state.qualityEpoch += 1;
        state.qualityHistory = [];
        state.qualityHistoryEpoch += 1;
        state.qualityDetailEpoch += 1;
        state.qualityPresenter?.invalidate(elements.qualityHistoryDetail);
        state.qualitySaveEpoch += 1;
        state.qualitySaving = false;
        elements.qualitySaveButton.disabled = false;
        elements.qualitySaveOutput.textContent = '只有点击“检查并保存报告”才写入不可变质量记录。';
        elements.qualityHistoryPublication.value = '';
        elements.qualityHistoryList.innerHTML = '<div class="output-box">审计历史尚未加载。</div>';
        elements.qualityHistoryDetail.innerHTML = '<div class="output-box">选择历史记录查看当时的观察；历史结果不代表当前图谱状态。</div>';
        state.activeDocuments = [];
        state.publicationCandidates = [];
        state.publicationCandidateOffset=0;
        state.publicationCandidateTotal=0;
        state.publicationCandidateEntityTotal=0;
        state.publicationCandidatesLoading=false;
        state.publicationBulkBusy=false;
        state.selectedPublicationCandidates.clear();
        state.selectedCandidateRevisions.clear();
        state.approvedRevisions.clear();
        elements.publicationRevisions.value = '';
        elements.publicationRemovals.value = '';
        state.reviewBusy=false;state.publicationBusy=false;renderPublicationCandidates();
        clearButtonFeedback(host);
        elements.inventorySummary.textContent = '已发布知识尚未加载。';
        elements.inventoryList.innerHTML = '<div class="output-box">已发布知识尚未加载。</div>';
        elements.qualityContent.innerHTML = '<div class="output-box">活动图谱质量尚未加载。</div>';
        elements.documentLifecycleSummary.textContent = '实时授权视图尚未加载。';
        elements.documentLifecycleList.innerHTML = '<div class="output-box">活动文档尚未加载。</div>';
        state.constructionBusy=false;state.expertImportBusy=false;state.expertPublishing=false;
        state.manualOperation=null;state.manualBusy=false;state.ontologySaving=false;
        clearOntologyInput();
        $('manual-output').textContent='人工补充保存后进入待确认列表。';
        $('manual-save-button').disabled=false;
        $('abox-publish-button').disabled=false;
        for(const input of host.querySelectorAll('input:not([type="checkbox"]),textarea')) input.value='';
        $('document-source').value='local-controlled-upload';$('document-language').value='zh-CN';
        $('document-family').value='';$('document-asset').value='';
        $('inventory-limit').value='100';
        delete $('document-extraction-mode').dataset.xmlMode;
        $('document-extraction-mode').value='LLM';
        $('document-knowledge-scope').disabled=false;
        setUploadKnowledgeScope('BUSINESS');
        lastBuildFlow='business';buildStep='upload';state.buildView=null;
        $('document-file-name').textContent='尚未选择文件';
        $('ontology-selection').open=false;
        $('construction-progress').hidden=true;$('construction-progress').textContent='';
        $('expert-abox').open=false;
        $('document-access-groups').replaceChildren();
        $('manual-subject-type').replaceChildren();$('manual-object-type').replaceChildren();$('manual-predicate').replaceChildren();
        $('foundation-status').textContent='';
        elements.ontologyList.replaceChildren();elements.historyList.replaceChildren();elements.constructionJobList.replaceChildren();
        elements.constructionOutput.textContent='';elements.publicationOutput.textContent='';
        elements.constructionOutput.hidden=true;elements.publicationOutput.hidden=true;
        $('construct-button').disabled=false;
        $('preview-chunks-button').disabled=false;
        $('document-chunk-max-chars').disabled=false;
        $('document-chunk-strategy').disabled=false;
        uploadMapping.sync();
        loadedIdentity=null;loadingIdentity=null;
      }

      $('ontology-clear').addEventListener('click', clearOntologyInput);
      elements.ontologyEditor.addEventListener('input', invalidateOntologyValidation);
      $('ontology-validate-button').addEventListener('click', foregroundAction('', validateOntologyEditor));
      $('ontology-file-button').addEventListener('click', () => $('ontology-file').click());
      $('ontology-file').addEventListener('change', loadOntologyFile);
      $('ontology-rule-file-button').addEventListener('click', () => $('ontology-rule-file').click());
      $('ontology-rule-file').addEventListener('change', loadOntologyRuleReferences);
      $('ontology-list-button').addEventListener('click', foregroundAction('正在读取本体…', loadOntologies));
      $('ontology-import-button').addEventListener('click', foregroundAction('', importOntology));
      $('abox-import-button').addEventListener('click', foregroundAction('', importABox));
      elements.aboxEditor.addEventListener('input', () => { invalidateExpertImportReceipt(); refreshABoxPreparation(); });
      $('abox-prepare-button').addEventListener('click', () => { if (setUploadKnowledgeScope('AUTHORITATIVE')) showConstructionFlow('business', 'source-upload-slot'); });
      $('abox-publish-button').addEventListener('click', foregroundAction('', publishImportedABox));
      $('abox-next-button').addEventListener('click', foregroundAction('', () => showConstructionFlow('business', 'business-upload-slot')));
      host.querySelectorAll('[data-build-view]').forEach(button => {
        button.addEventListener('click', foregroundAction('', () => selectBuildView(button.dataset.buildView)));
        button.addEventListener('keydown', event => {
          if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
          event.preventDefault();
          const view = event.key === 'Home' ? 'ontology' : event.key === 'End' ? 'instances' : button.dataset.buildView === 'ontology' ? 'instances' : 'ontology';
          selectBuildView(view);
          $(`build-tab-${view}`).focus();
        });
      });
      $('inspect-build-ontology').addEventListener('click', foregroundAction('', () => selectBuildView('ontology')));
      $('document-tbox').addEventListener('input', () => {uploadMapping.clear();renderConstructionOntology();});
      $('document-knowledge-scope').addEventListener('change', event => setUploadKnowledgeScope(event.currentTarget.value));
      host.querySelectorAll('[data-flow-go]').forEach(button => button.addEventListener('click', foregroundAction('', () => showConstructionFlow(button.dataset.flowGo, button.dataset.flowTarget))));

      $('construct-button').addEventListener('click', constructKnowledge);
      $('preview-chunks-button').addEventListener('click', () => constructKnowledge(null,{previewOnly:true}));
      for (const id of ['document-chunk-max-chars','document-chunk-strategy']) $(id).addEventListener('change', () => {
        if (state.constructionBusy) return;
        clearConstructionResult();
        completeConstructionOperation();
        updateChunkingSummary();
      });
      $('document-extraction-mode').addEventListener('change', updateConstructionMode);
      $('upload-demo-prepare').addEventListener('click', foregroundAction('', () => prepareDemoUpload($('document-knowledge-scope').value === 'AUTHORITATIVE' ? 'authoritative_source' : 'maintenance_report')));
      $('construction-jobs-refresh-button').addEventListener('click', foregroundAction('正在读取构建任务…', loadConstructionJobs));
      $('manual-fact-panel').addEventListener('toggle', () => { if ($('manual-fact-panel').open) refreshManualForm(); });
      $('manual-kind').addEventListener('change', refreshManualForm);
      $('manual-subject-type').addEventListener('change', refreshManualForm);
      $('manual-save-button').addEventListener('click', foregroundAction('', submitManualFact));
      $('review-refresh-button').addEventListener('click', foregroundAction('正在刷新审核与匹配…', () => loadReviews({refreshResolutions: true})));
      $('review-approve-button').addEventListener('click', foregroundAction('', () => submitReviews('APPROVED', chosenReviews())));
      $('review-reject-button').addEventListener('click', foregroundAction('', () => submitReviews('REJECTED', chosenReviews())));
      $('review-quarantine-button').addEventListener('click', foregroundAction('', () => submitReviews('QUARANTINED', chosenReviews())));
      $('publication-button').addEventListener('click', foregroundAction('', publishKnowledge));
      $('publication-preview-button').addEventListener('click', foregroundAction('', previewKnowledgePublication));
      elements.publicationRevisions.addEventListener('input',syncPublicationTextSelection);
      elements.publicationRemovals.addEventListener('input',invalidatePublicationPreview);
      $('publication-candidates-refresh-button').addEventListener('click', foregroundAction('正在读取待发布内容…', loadPublicationCandidates));
      $('history-refresh-button').addEventListener('click', foregroundAction('正在读取发布历史…', loadHistory));
      $('inventory-export-button').addEventListener('click', foregroundAction('', exportInventory));
      $('inventory-refresh-button').addEventListener('click', foregroundAction('正在读取已发布知识…', loadInventory));
      $('inventory-add-removals-button').addEventListener('click', addInventoryRemovals);
      elements.inventoryDocumentFilter.addEventListener('keydown', event => {
        if (event.key === 'Enter') loadInventory();
      });
      $('quality-refresh-button').addEventListener('click', foregroundAction('正在检查知识质量…', loadQuality));
      elements.qualitySaveButton.addEventListener('click', foregroundAction('', saveQualityRun));
      $('quality-history-refresh-button').addEventListener('click', foregroundAction('正在读取质量检查历史…', loadQualityHistory));
      elements.qualityHistoryPublication.addEventListener('keydown', event => {
        if (event.key === 'Enter') loadQualityHistory();
      });
      $('document-lifecycle-refresh-button').addEventListener('click', foregroundAction('正在读取来源状态…', loadActiveDocuments));
      $('document-file').addEventListener('change', event => {
        selectUploadFile(event.currentTarget.files?.[0]);
      });

      const componentsReady = Promise.all([import('/industrial/assets/knowledge/browser.mjs'),import('/industrial/assets/knowledge/maintenance.mjs'),import('/industrial/assets/knowledge/maintenance-actions.mjs')]).then(([{mountBrowser},{mountMaintenance},{mountActions}]) => {
        state.knowledgeBrowser = mountBrowser({feedback:beginButtonFeedback,api: apiRequest, epoch: () => state.identityEpoch, onExplore, onSource,
          maintain: item => state.maintenanceActions.maintain(item)});
        state.qualityPresenter=mountMaintenance({feedback:beginButtonFeedback,api:apiRequest,epoch:()=>state.identityEpoch,browser:state.knowledgeBrowser,navigate:showConstructionFlow,correct:item=>state.maintenanceActions.maintain(item)});
        if(state.quality)renderQuality();
        state.maintenanceActions=mountActions({feedback:beginButtonFeedback,api:apiRequest,epoch:()=>state.identityEpoch,browser:state.knowledgeBrowser,navigate:showConstructionFlow,correctRecord:correctPublishedRecord,toast:showToast,maintainSource:async item=>{const identity=state.identityEpoch;showConstructionFlow('maintenance','maintenance-document-lifecycle');elements.inventoryDocumentFilter.value=item.document_id;await loadActiveDocuments();if(identity!==state.identityEpoch)return;const row=[...elements.documentLifecycleList.querySelectorAll('[data-maintenance-document]')].find(el=>el.dataset.maintenanceDocument===item.document_id);if(row){row.scrollIntoView({behavior:'smooth',block:'center'});row.setAttribute('tabindex','-1');row.focus();showToast(`已定位来源：${item.title}`);}else showToast('所选来源不在当前可维护的 100 份资料中，请核对权限与来源状态。');},rollback:async(target,expected)=>{const index=state.publications.findIndex(p=>p.publication_id===target);if(index<0)throw new Error('目标版本已变化，请刷新。');await rollbackPublication(index,expected);}});
        renderInventory();renderHistory();
        if (state.constructionFlow === 'browse') state.knowledgeBrowser.activate();
      }).catch(() => { $('kb-summary').textContent = '知识浏览组件加载失败，请刷新页面重试。'; });


  $('document-family').addEventListener('change',()=>{
    const family=$('document-family').value;
    if(family) $('document-tbox').value='industrial-electric-v1';
    const file=$('document-file').files?.[0];
    if(file) $('document-uri').value=defaultDocumentUri(file.name.toLowerCase().replace(/[^a-z0-9._-]+/g,'-')||'document');
  });
  async function refreshCapabilities() {
    if(!client.session) return;
    const identity=client.epoch;
    if(loadedIdentity===identity) return;
    if(loadingIdentity?.identity===identity) return loadingIdentity.promise;
    const promise=(async()=>{
      renderDocumentAccessGroups();
      host.querySelectorAll('[data-industrial-upload]').forEach(node=>node.hidden=currentPersona()?.tenant_id!=='industrial-schneider-demo');
      renderDemoKit();
      const caps=bootstrap.capabilities||{},provider=caps.extraction_provider||{};
      $('provider-note').textContent=provider.model?`抽取模型：${provider.model}。抽取后需要审核与发布。`:'当前服务未配置自动抽取，可使用仅保留来源模式。';
      $('construction-cost-note').textContent='构建受文档大小、分块数量、模型调用与超时限制。详细结果和失败原因可在构建任务中查看。';
      await loadOntologies();
      if(identity!==client.epoch) return;
      const active=state.ontologies.find(item=>item.status==='PUBLISHED');
      $('document-tbox').value=active?.key||'';
      syncOntologyControls();
      renderConstructionOntology();
      renderBuildView();
      refreshABoxPreparation();updateConstructionMode();
      await Promise.allSettled([loadConstructionJobs(),loadReviews(),loadPublicationCandidates(),loadHistory()]);
      if(identity!==client.epoch) return;
      loadedIdentity=identity;
    })();
    loadingIdentity={identity,promise};
    try {await promise;} finally {if(loadingIdentity?.identity===identity) loadingIdentity=null;}
  }
  function activate(name) {
    if(name==='build') showConstructionFlow(lastBuildFlow);
    else if(name==='records') showConstructionFlow('browse');
    else if(name==='maintenance') {
      showConstructionFlow('maintenance');
      void Promise.allSettled([loadInventory(),loadActiveDocuments(),loadHistory()]);
    }
    void refreshCapabilities();
  }
  await componentsReady;
  return {reset,refreshCapabilities,activate,async openEntity(id){
    showConstructionFlow('browse');
    await state.knowledgeBrowser?.select(id);
  }};
}
