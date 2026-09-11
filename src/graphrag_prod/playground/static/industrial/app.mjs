import {
  $,
  element,
  button,
  clear,
  tag,
  metadata,
  authorityTag,
  sourceTag,
  sourceLink,
  TYPE_LABELS,
  PREDICATE_LABELS,
  ORIGIN_LABELS,
  STATUS_LABELS,
  graphScope,
  assetLabel,
  dateLabel,
  shortId,
  evidenceQuote,
  toast,
  message,
  empty,
  safeError,
  WorkbenchClient,
  beginButtonFeedback, clearButtonFeedback,
} from "./core.mjs";
import { IndustrialGraph } from "./graph.mjs";
import { TYPE_COLORS, VIEWS, samePin, graphEmptyState, ontologyGraphFilters } from "./graph-model.mjs";
import { mountGovernance } from "./governance.mjs";
import { SourceCatalog } from "./source-catalog.mjs";
import { mountRetrievalOptions, rerankingLabel } from "./retrieval-options.mjs";
import { mountWorkspacePicker } from "./workspaces.mjs";

const client = new WorkbenchClient();
let retrievalOptions, workspacePicker;
let graphFilters = [];
let graph,
  construction,
  sourceCatalog,
  bootstrap,
  page = null,
  pageQuery = null,
  view = "all",
  graphEpoch = 0,
  selectionEpoch = 0,
  searchEpoch = 0,
  sourcesEpoch = 0,
  searchBusy = false,
  panel = "explore",
  graphSource = null,
  searchSource = null,
  searchContexts = [];
const panelNames = {
  explore: "图谱探索",
  search: "证据检索",
  sources: "来源资料",
  build: "知识构建",
  records: "知识档案",
  maintenance: "质量与维护",
};
function resetInspector() {
  selectionEpoch++;
  $("graph-inspector").open = false;
  $("graph-inspector").hidden = true;
  $("inspector-summary").textContent = "详情与来源";
  $("inspector-content").replaceChildren();
}
function openInspector(label) {
  $("inspector-summary").textContent = `${label || "所选内容"} · 详情与来源`;
  $("graph-inspector").hidden = false;
  $("graph-inspector").open = true;
}
function viewSelectedDetails() {
  const inspector = $("graph-inspector");
  if (inspector.hidden) return;
  inspector.open = true;
  const summary = $("inspector-summary");
  summary.scrollIntoView({ block: "start", behavior: "auto" });
  summary.focus({ preventScroll: true });
}
function loadingGraph(
  text = "正在载入有来源的知识关系",
  detail = "构建当前身份可见的图谱视图",
  busy = true,
) {
  const placeholder = $("graph-placeholder");
  placeholder.hidden = busy;
  placeholder.querySelector(".graph-empty-actions")?.remove();
  placeholder.querySelector("strong").textContent = text;
  placeholder.querySelector("p").textContent = detail;
  placeholder.querySelector(".loading-orbit").hidden = !busy;
}
function resetGraph() {
  graphEpoch++;
  page = null;
  pageQuery = null;
  graph?.clear();
  resetInspector();
  $("node-count").textContent = "—";
  $("edge-count").textContent = "—";
  $("type-count").textContent = "—";
  $("type-legend").replaceChildren();
  $("graph-timing").textContent = "";
  $("next-page").hidden = true;
}
function updateStats(result) {
  $("node-count-label").textContent =
    view === "ontology" ? "声明的节点类型" : "当前可见节点";
  $("edge-count-label").textContent =
    view === "ontology" ? "允许的类型连接" : "有证据的关系";
  document.querySelector(".graph-key").hidden = view === "ontology";
  $("type-count").parentElement.hidden = view === "ontology";
  $("node-count").textContent = result.nodes;
  $("edge-count").textContent = result.edges;
  $("type-count").textContent =
    view === "ontology"
      ? (page.schema.entity_types || []).length
      : new Set(page.nodes.map((n) => n.entity_type)).size;
  const legend = clear($("type-legend"));
  const types =
    view === "ontology"
      ? page.schema.entity_types.map((t) => t.name)
      : [...new Set(page.nodes.map((n) => n.entity_type))];
  for (const type of types) {
    const item = element("span", "legend-item"),
      dot = element("i", "legend-swatch");
    dot.style.backgroundColor = (TYPE_COLORS[type] || [])[1] || "#a2b29a";
    item.append(dot, document.createTextNode(TYPE_LABELS[type] || type));
    legend.append(item);
  }
  $("canvas-caption").textContent =
    view === "ontology"
      ? "本体模型 · 点选连线查看关系约束，不代表实例事实"
      : `${selectedGraphFilter()?.label || VIEWS[view].label} · 滚轮缩放 · 拖动节点 · 点选后在图谱下方查看详情与来源`;
}
function currentPersona() {
  return (bootstrap?.personas || []).find(item => item.id === client.session?.identity?.id);
}
function isIndustrialIdentity() {
  return currentPersona()?.tenant_id === (bootstrap?.industrial?.tenant_id || "industrial-schneider-demo");
}
function selectedIndustrialScope(family, asset) {
  return isIndustrialIdentity() && (family || asset) ? graphScope(family, asset) : null;
}
function updateReferenceControl() {
  $("search-references").disabled = !selectedIndustrialScope($("search-family").value, $("search-asset").value);
}
function sourceVersionFilter(source) {
  return { document_ids: [source.document_id], version_ids: [source.version_id] };
}
function selectedGraphFilter() {
  if (view === "ontology") return null;
  return graphFilters.find(item => item.value === $("graph-relation-filter").value) || null;
}
function syncGraphFilters(schema) {
  const select = $("graph-relation-filter"), selected = select.value;
  graphFilters = ontologyGraphFilters(schema);
  clear(select).append(new Option("全部关系", ""));
  for (const label of ["本体层级", "关系类型"]) {
    const items = graphFilters.filter(item => item.group === label);
    if (!items.length) continue;
    const group = element("optgroup"); group.label = label;
    for (const item of items) group.append(new Option(item.label, item.value));
    select.append(group);
  }
  select.value = graphFilters.some(item => item.value === selected) ? selected : "";
  select.disabled = view === "ontology" || !graphFilters.length;
  return Boolean(selected && !select.value);
}
function graphBody(extra = {}) {
  const scope = selectedIndustrialScope($("family").value, $("asset").value);
  return {
    ...(scope ? { industrial_scope: scope } : {}),
    ...(graphSource ? { version_filter: sourceVersionFilter(graphSource) } : {}),
    trust_policy: $("trust").value,
    entity_types: [],
    predicates: selectedGraphFilter()?.predicates || [],
    name_query: null,
    seed_entity_ids: [],
    direction: "both",
    hops: 1,
    page_size: 100,
    ...extra,
  };
}
async function loadGraph(extra = {}, trigger = $('reload-graph')) {
  const finish=beginButtonFeedback(trigger);
  const epoch = ++graphEpoch;
  selectionEpoch++;
  const identity = client.epoch;
  graph?.clear();
  resetInspector();
  page = null;
  $("node-count").textContent = "—";
  $("edge-count").textContent = "—";
  $("type-count").textContent = "—";
  $("type-legend").replaceChildren();
  $("graph-timing").textContent = "";
  $("next-page").hidden = trigger !== $('next-page');
  loadingGraph();
  const started = performance.now();
  const query = graphBody(extra);
  try {
    const result = await client.request("/v1/knowledge/graph:query", query);
    if (epoch !== graphEpoch || identity !== client.epoch) return;
    if (syncGraphFilters(result.schema) && view !== "ontology") {
      await loadGraph({}, trigger);
      return;
    }
    page = result;
    pageQuery = query;
    const counts = graph.setPage(page, view);
    updateStats(counts);
    $("graph-timing").textContent =
      `${Math.round(performance.now() - started)} ms`;
    $("next-page").hidden = !page.page.has_more || view === "ontology";
    if (counts.nodes) {
      $("graph-placeholder").hidden = true;
    } else {
      const state = graphEmptyState(view, page.pin, selectedGraphFilter()?.label);
      loadingGraph(state.title, state.detail, false);
      if (state.showAll) {
        const actions = element("div", "graph-empty-actions");
        actions.append(button("查看全部关系", () => {
          view = "all";
          $("graph-relation-filter").value = "";
          document.querySelectorAll("[data-view]").forEach(item => item.classList.toggle("selected", item.dataset.view === view));
          void loadGraph();
        }));
        $("graph-placeholder").append(actions);
      }
    }
    if (extra.seed_entity_ids?.length) toast("已显示所选节点周围的授权关系。");
  } catch (error) {
    if (epoch !== graphEpoch || identity !== client.epoch) return;
    const text = safeError(error);
    if (text) {
      loadingGraph("图谱暂未载入", text, false);
      message(text, true);
    }
  } finally {finish();}
}
function detailField(label, value) {
  const n = element("div", "detail-field");
  n.append(element("span", "", label), element("span", "", value ?? "未记录"));
  return n;
}
function selectedDescription(selected) {
  if (selected.kind === "node")
    return (
      selected.entity?.label || selected.definition?.name || selected.label
    );
  const edge = selected.assertion;
  return edge
    ? `${PREDICATE_LABELS[edge.predicate] || edge.predicate}`
    : selected.definition?.name;
}
function renderSchema(selected) {
  const target = clear($("inspector-content"));
  const definition = selected.definition;
  target.append(
    element("div", "type-caption", "本体声明"),
    element(
      "h2",
      "",
      TYPE_LABELS[definition.name] ||
        PREDICATE_LABELS[definition.name] ||
        definition.name,
    ),
    element("div", "record-key", definition.name),
    element("p", "schema-props", definition.description || "没有说明。"),
  );
  const note = element(
    "div",
    "source-note",
    "此处表示允许的类型与关系约束，不表示某台设备已经具有这些事实。层级布局由视图计算。",
  );
  target.append(note);
  if (definition.source_types)
    target.append(
      detailField(
        "源类型",
        definition.source_types.map((t) => TYPE_LABELS[t] || t).join("、"),
      ),
      detailField(
        "目标类型",
        definition.target_types.map((t) => TYPE_LABELS[t] || t).join("、"),
      ),
    );
  if (definition.properties?.length) {
    const section = element("div", "detail-section");
    section.append(element("h3", "", "允许的属性"));
    for (const prop of definition.properties)
      section.append(
        detailField(
          prop.name,
          `${prop.datatype}${prop.unit ? " · " + prop.unit : ""} · ${prop.required ? "必填" : "可选"}`,
        ),
      );
    target.append(section);
  }
  const hierarchies = (page?.schema?.hierarchies || []).filter(
    (h) =>
      h.relationship_type === definition.name ||
      h.predicate === definition.name ||
      h.entity_types?.includes(definition.name) ||
      h.allowed_entity_types?.includes(definition.name),
  );
  if (hierarchies.length)
    for (const h of hierarchies)
      target.append(element("p", "source-note", h.description || h.name));
}
async function selectGraph(selected) {
  if (!selected) {
    resetInspector();
    return;
  }
  if (selected.definition) {
    selectionEpoch++;
    openInspector(selectedDescription(selected));
    renderSchema(selected);
    return;
  }
  const current = page,
    epoch = graphEpoch,
    identity = client.epoch,
    selection = ++selectionEpoch;
  if (!current) return;
  openInspector(selectedDescription(selected));
  const target = clear($("inspector-content"));
  const entity = selected.entity,
    assertion = selected.assertion;
  target.append(
    element(
      "div",
      "type-caption",
      entity
        ? TYPE_LABELS[entity.entity_type] || entity.entity_type
        : "有来源的关系",
    ),
    element("h2", "", selectedDescription(selected)),
  );
  const tags = element("div", "tag-row");
  for (const authority of entity?.authority_levels || assertion?.authority_levels || [
    assertion?.authority_level,
  ])
    if (authority) tags.append(authorityTag(authority));
  if (assertion) {
    tags.append(tag(`${assertion.source_count || 1} 条来源记录，等级分别保留`));
  }
  target.append(tags);
  if (entity) {
    const identifiers = element("details", "graph-record-identifiers");
    identifiers.append(element("summary", "", "记录标识"), element("div", "record-key", entity.canonical_key));
    target.append(identifiers);
    const expand = button("展开一跳关系 →", () =>
      loadGraph({
        view_token: current.view_token,
        seed_entity_ids: [entity.entity_id],
        hops: 1,
      }),
    );
    const section = element("div", "detail-section");
    section.append(expand, button("打开实体档案 →", async () => {
      if (graph?.isFullscreen()) await document.exitFullscreen();
      navigate("records", true);
      await construction?.openEntity(entity.entity_id);
    }));
    target.append(section);
    const literals = current.literals.filter(
      (item) => item.subject === entity.entity_id,
    );
    if (literals.length) {
      const values = element("details", "detail-section graph-properties");
      values.append(element("summary", "", `属性记录 · ${literals.length} 条`));
      for (const item of literals) {
        const row = element("div", "literal-row");
        row.append(
          element("span", "", item.predicate),
          button(item.value, () => showLiteral(item, current), "text-button"),
        );
        values.append(row);
      }
      target.append(values);
    }
  } else if (assertion) {
    const byId = new Map(current.nodes.map((n) => [n.entity_id, n]));
    target.append(
      detailField("源节点", byId.get(assertion.source)?.label),
      detailField(
        "关系",
        `${assertion.predicate} · ${PREDICATE_LABELS[assertion.predicate] || ""}`,
      ),
      detailField("目标节点", byId.get(assertion.target)?.label),
    );
    if (assertion.predicate === "MAY_INDICATE")
      target.append(
        element(
          "div",
          "source-note",
          "这是候选关联，需要结合检查与排除证据；不能单凭此连线确认故障原因。",
        ),
      );
  }
  const revisions = entity
    ? entity.mention_revision_ids
    : assertion.revision_ids || [assertion.revision_id];
  const evidenceArea = element("div", "detail-section");
  evidenceArea.append(
    element("h3", "", "来源与事实依据"),

  );
  const evidenceRead=button('读取来源依据',()=>loadEvidence(0,evidenceRead));
  target.append(evidenceRead,evidenceArea);
  let evidenceSerial = 0;
  async function loadEvidence(offset=0,trigger=evidenceRead) {
    const finish=beginButtonFeedback(trigger);
    const serial = ++evidenceSerial;

    try {
      const data = await client.request("/v1/knowledge/graph:evidence", {
        view_token: current.view_token,
        revision_ids: revisions.slice(offset, offset + 10),
      });
      if (
        serial !== evidenceSerial || epoch !== graphEpoch ||
        identity !== client.epoch ||
        selection !== selectionEpoch
      )
        return;
      if (!samePin(current.pin, data.pin))
        throw new Error("图谱与来源版本不一致，请刷新图谱。");
      clear(evidenceArea);
      evidenceArea.append(element("h3", "", "来源与事实依据"));
      if (!data.items.length)
        evidenceArea.append(empty("当前身份没有可见的来源记录。"));
      for (const item of data.items) evidenceArea.append(renderEvidence(item));
      evidenceArea.append(element("p", "muted", `来源记录 ${offset+1}–${Math.min(offset+10,revisions.length)} / ${revisions.length}`));
      if(offset) evidenceArea.append(button("上一页来源",event=>loadEvidence(offset-10,event.currentTarget)));
      if(offset+10<revisions.length) evidenceArea.append(button("下一页来源",event=>loadEvidence(offset+10,event.currentTarget)));
    } catch (error) {
      if (
        serial !== evidenceSerial || epoch !== graphEpoch ||
        identity !== client.epoch ||
        selection !== selectionEpoch
      )
        return;
      const text = safeError(error);
      if (text) {
        clear(evidenceArea).append(element("p", "danger-text", text));
      }
    } finally {finish();}
  }
  await loadEvidence();
}
function renderEvidence(item) {
  const node = element("article", "evidence-detail");
  const evidence = item.evidence,
    citation = evidence.citation;
  const tags = element("div", "tag-row");
  tags.append(
    sourceTag(item.source_kind),
    authorityTag(item.authority_level),
    tag(STATUS_LABELS[item.status] || item.status),
  );
  if(item.fact_distinction) {
    node.append(element("p", "source-note", `人工独立事实（区分依据尚未结构化）：${item.fact_distinction.reason}。审核人：${item.fact_distinction.reviewed_by} · ${item.fact_distinction.reviewed_at}。这是审核判断，不是来源原文。`));
  }
  node.append(tags, element("h3", "", citation.document_title));
  node.append(
    metadata([
      citation.section,
      citation.page_number ? `原文第 ${citation.page_number} 页` : null,
      `文档 v${citation.version_number}`,
      dateLabel(citation.published_at),
    ]),
  );
  node.append(
    evidenceQuote(citation, evidence),
    detailField("产生方式", ORIGIN_LABELS[item.origin] || item.origin),
    detailField(
      "记录置信度",
      `${Math.round(item.confidence * 100)}% · 不代表诊断概率`,
    ),
    detailField("复核者", item.reviewed_by || "未记录"),
    detailField(
      "复核日期",
      item.reviewed_at ? dateLabel(item.reviewed_at) : "未记录",
    ),
  );
  if (item.review_notes) node.append(element("p", "", item.review_notes));
  if (item.applicability?.is_synthetic)
    node.append(
      element(
        "p",
        "source-note",
        "合成设备记录，用于项目验证，不是真实现场故障记录。",
      ),
    );
  if (item.applicability?.project_curated_not_company_approved)
    node.append(
      element("p", "source-note", "项目整理参考材料，尚未获得企业专家认证。"),
    );
  node.append(
    metadata([
      `片段 ${citation.ordinal + 1} · 字符 ${citation.char_start}–${citation.char_end}`,
      `来源校验 ${shortId(citation.version_checksum)}`,
    ]),
  );
  const link = sourceLink(citation.canonical_uri);
  if (link) node.append(link);
  return node;
}
async function showLiteral(item, current) {
  openInspector(PREDICATE_LABELS[item.predicate] || item.predicate);
  const epoch = graphEpoch,
    identity = client.epoch,
    selection = ++selectionEpoch;
  const target = clear($("inspector-content"));
  target.append(
    element("div", "type-caption", "有来源的属性"),
    element("h2", "", item.predicate),
    element("p", "", item.value),
  );
  try {
    const payload = await client.request("/v1/knowledge/graph:evidence", {
      view_token: current.view_token,
      revision_ids: [item.revision_id],
    });
    if (
      epoch !== graphEpoch ||
      identity !== client.epoch ||
      selection !== selectionEpoch
    )
      return;
    if (!samePin(current.pin, payload.pin))
      throw new Error("图谱与来源版本不一致，请刷新。");
    for (const entry of payload.items) target.append(renderEvidence(entry));
  } catch (error) {
    if (selection === selectionEpoch && epoch === graphEpoch) {
      const text = safeError(error);
      if (text) target.append(element("p", "danger-text", text));
    }
  }
}

async function loadSources({ populateAssets = false, autoSelectAsset = false } = {}) {
  if (!populateAssets)
    return sourceCatalog?.load({ family: isIndustrialIdentity() ? $("sources-family").value || null : null });
  const epoch = ++sourcesEpoch, identity = client.epoch;
  const selected = $("asset").value;
  clear($("asset")).append(new Option("当前范围的可见设备", ""));
  if (!isIndustrialIdentity()) return;
  try {
    const result = await client.request("/v1/industrial/sources:query", {
      family: $("family").value || null, asset_keys: [], limit: 100,
    });
    if (epoch !== sourcesEpoch || identity !== client.epoch) return;
    const keys = [...new Set(result.sources.flatMap(row => row.asset_keys))].sort();
    for (const key of keys) $("asset").append(new Option(assetLabel(key), key));
    if (keys.includes(selected)) $("asset").value = selected;
    else if (autoSelectAsset && $("family").value && keys.length) $("asset").value = keys[0];
    if (autoSelectAsset) $("search-asset").value = $("asset").value ? assetLabel($("asset").value) : "";
    if (result.has_more) $("asset").title = "设备选项来自本次可见来源目录，选择产品族可进一步缩小范围。";
    else $("asset").title = "";
  } catch (error) {
    if (epoch !== sourcesEpoch || identity !== client.epoch) return;
    const text = safeError(error);
    if (text) toast(`设备目录暂未载入：${text}。仍可使用全部知识范围。`);
  }
}
function updateSourceScope(kind) {
  const source = kind === "graph" ? graphSource : searchSource;
  const target = $(`${kind}-document-scope`);
  if (!target) return;
  clear(target);
  target.hidden = !source;
  if (!source) return;
  target.append(element("span", "", `当前资料：${source.title || "指定文档"} · 版本 ${shortId(source.version_id)} `),
    button("恢复全部资料", () => {
      if (kind === "graph") { graphSource = null; updateSourceScope(kind); void loadGraph(); }
      else { searchSource = null; updateSourceScope(kind); resetSearchScope(); }
    }, "text-button"));
}
async function selectSource(source, action) {
  if (action === "search") {
    searchSource = source;
    $("search-family").value = "";
    $("search-asset").value = "";
    updateReferenceControl();
    updateSourceScope("search");
    resetSearchScope();
    navigate("search");
    $("question").focus();
    return;
  }
  graphSource = source;
  $("family").value = "";
  $("asset").value = "";
  view = "all";
  $("graph-relation-filter").value = "";
  document.querySelectorAll("[data-view]").forEach(item => item.classList.toggle("selected", item.dataset.view === view));
  updateSourceScope("graph");
  navigate("explore");
  await loadGraph();
}
function resetSearchScope() {
  searchEpoch++;
  $("search-diagnostics")?.replaceChildren();
  searchContexts = [];
  if ($("copy-contexts")) $("copy-contexts").disabled = true;
  clear($("search-results"));
  $("search-summary").textContent = searchBusy
    ? "上一个检索仍在处理；范围已更新，完成后可重新检索。"
    : "";
}
function navigate(name, fromGovernance = false) {
  if (!panelNames[name]) return;
  if(["build","maintenance"].includes(name) && client.session && !client.session.identity.scopes?.includes("knowledge:publish")) {
    toast("用户身份可检索和浏览知识，构建维护请切换管理员。");return;
  }
  panel = name;
  for (const [id, label] of Object.entries(panelNames)) {
    const active = id === name;
    const section = $(`panel-${id}`);
    if (section) { section.hidden = !active; section.classList.toggle("active", active); }
    document.querySelectorAll(`[data-panel="${id}"]`).forEach(item => item.classList.toggle("active", active));
    if (active) $("breadcrumb-current").textContent = label;
  }
  if (["build", "records", "maintenance"].includes(name)) {
    const host = $("governance-host"), slot = $(`governance-slot-${name}`);
    if (host && slot && host.parentElement !== slot) slot.append(host);
    if (!fromGovernance) void construction?.activate(name);
  }
  if (name === "explore") requestAnimationFrame(() => graph?.fit());
  if (name === "sources") void loadSources();
}
async function search(event) {
  event.preventDefault();
  if (searchBusy) return;
  const identity = client.epoch,
    epoch = ++searchEpoch;
  const query = $("question").value.trim();
  if (!query) return;
  searchBusy = true;
  searchContexts = [];
  if ($("copy-contexts")) $("copy-contexts").disabled = true;
  const submit = $("search-submit");
  submit.disabled = true;
  submit.classList.add("busy");
  $("search-summary").textContent = "";
  clear($("search-results"));
  $("search-diagnostics")?.replaceChildren();
  const start = performance.now();
  try {
    const scope = selectedIndustrialScope($("search-family").value, $("search-asset").value);
    if (scope) scope.include_references = $("search-references").checked;
    const options=retrievalOptions?.requestOptions() || {include_graph:false};
    if(searchSource) {
      const filter=options.version_filter||{};
      for(const [key,id] of [["document_ids",searchSource.document_id],["version_ids",searchSource.version_id]]) {
        if(filter[key]?.length && !filter[key].includes(id)) throw new Error("检索选项与当前所选资料范围不一致，请清除资料筛选或调整文档与版本 ID。");
      }
      options.version_filter={...filter,...sourceVersionFilter(searchSource)};
    }
    const result = await client.request("/v1/retrieval", {
      query_text: query,
      ...(scope ? { industrial_scope: scope } : {}),
      ...options,
    });
    if (identity !== client.epoch || epoch !== searchEpoch) return;
    retrievalOptions?.renderDiagnostics(result,$("search-diagnostics"));
    const projected=result.graph?.entities||[];
    if(projected.length) {
      const links=element("div","retrieval-graph-links");
      links.append(element("span","muted",`相关已发布实体 · ${projected.length}`));
      for(const entity of projected.slice(0,20)) links.append(button(entity.canonical_name||entity.entity_id,()=>construction.openEntity(entity.entity_id),"text-button"));
      $("search-results").append(links);
    }
    const rows = result.chunks || [];
    searchContexts = rows;
    if ($("copy-contexts")) $("copy-contexts").disabled = !rows.length;
    $("search-summary").textContent =
      `${rows.length} 条来源片段 · ${(performance.now() - start) / 1000 < 1 ? Math.round(performance.now() - start) + " ms" : ((performance.now() - start) / 1000).toFixed(1) + " s"} · ${rerankingLabel(result.trace)}`;
    if (!rows.length)
      $("search-results").append(
        empty(
          "当前资料与权限范围内未找到可用证据。可补充设备编码、调整范围或上传资料。",
        ),
      );
    rows.forEach((chunk, index) => {
      const citation = chunk.citation;
      const card = element("article", "evidence-card");
      const head = element("div", "evidence-card-head");
      const body = element("div");
      body.append(
        element("h3", "", citation.document_title),
        metadata([
          citation.section,
          citation.page_number ? `原文第 ${citation.page_number} 页` : null,
          `文档 v${citation.version_number}`,
          dateLabel(citation.published_at),
        ]),
      );
      head.append(
        element("span", "rank-mark", String(index + 1).padStart(2, "0")),
        body,
      );
      card.append(
        head,
        element("pre", "", chunk.text),
        metadata([
          `片段 ${citation.ordinal + 1} · 字符 ${citation.char_start}–${citation.char_end}`,
          `引用 ${shortId(citation.chunk_id)}`,
          `来源校验 ${shortId(citation.version_checksum)}`,
        ]),
      );
      const actions = element("div", "evidence-actions");
      actions.append(button("查看原文与前后片段", () => sourceCatalog.open({
        document_id: citation.document_id, version_id: citation.version_id,
        title: citation.document_title,
      }, citation.ordinal), "text-button"));
      const link = sourceLink(citation.canonical_uri);
      if (link) actions.append(link);
      card.append(actions);
      $("search-results").append(card);
    });
  } catch (error) {
    if (identity === client.epoch && epoch === searchEpoch) {
      const text = safeError(error);
      if (text) {
        $("search-summary").textContent = text;
        $("search-results").append(empty(text));
      }
    }
  } finally {
    if (identity === client.epoch) {
      searchBusy = false;
      submit.disabled = false;
      submit.classList.remove("busy");
    }
  }
}
async function copyContexts() {
  if (!searchContexts.length) return;
  const context = searchContexts.map((chunk, index) => {
    const citation = chunk.citation;
    return `[${index + 1}] ${citation.document_title}\n文档版本：${citation.version_id}\nChunk：${citation.chunk_id} · 字符 ${citation.char_start}–${citation.char_end}\n${chunk.text}`;
  }).join("\n\n");
  try {
    await navigator.clipboard.writeText(context);
    toast("已复制当前检索上下文及来源位置。");
  } catch {
    toast("浏览器未允许复制，请直接选择检索片段中的文字。");
  }
}
async function changePersona() {
  graphFilters = [];
  clear($("graph-relation-filter")).append(new Option("全部关系", ""));
  $("graph-relation-filter").disabled = true;
  message();
  resetGraph();
  sourcesEpoch++;
  sourceCatalog?.reset();
  graphSource = null;
  searchSource = null;
  searchContexts = [];
  updateSourceScope("graph");
  updateSourceScope("search");
  if ($("copy-contexts")) $("copy-contexts").disabled = true;
  searchEpoch++;
  searchBusy = false;
  clearButtonFeedback();
  clear($("search-results")).append(
    empty("输入问题后，相关片段会显示在这里。"),
  );
  clear($("sources-list"));
  $("search-summary").textContent = "";
  $("search-submit").disabled = false;
  $("search-submit").classList.remove("busy");
  for (const modal of document.querySelectorAll("dialog")) modal.close();
  clear($("asset")).append(new Option("当前产品族的可见设备", ""));
  construction?.reset();
  retrievalOptions?.reset();
  loadingGraph();
  try {
    await client.selectPersona($("persona").value);
    const identity = client.epoch;
    $("service-status").textContent = "本地服务已连接";
    const canManage=client.session?.identity?.scopes?.includes("knowledge:publish");
    for(const name of ["build","maintenance"]) document.querySelectorAll(`[data-panel="${name}"]`).forEach(node=>node.hidden=!canManage);
    if(!canManage && ["build","maintenance"].includes(panel))navigate("explore");
    workspacePicker?.updateControls();
    const industrial = isIndustrialIdentity();
    $("asset").closest("label").hidden = !industrial;
    for (const id of ["family", "asset", "search-family", "search-asset", "sources-family", "search-references"])
      $(id).disabled = !industrial;
    for (const id of ["family", "asset", "search-family", "search-asset", "sources-family"])
      $(id).value = "";
    updateReferenceControl();
    document.querySelectorAll("[data-question][data-family]").forEach(item => { item.hidden = !industrial; });
    if(canManage)await construction?.refreshCapabilities();
    if (identity !== client.epoch) return false;
    await loadSources({ populateAssets: true });
    if (identity !== client.epoch) return;
    await loadGraph();
    if (panel === "sources") await loadSources();
    return true;
  } catch (error) {
    const text = safeError(error);
    if (text) {
      message(text, true);
      $("service-status").textContent = "连接未完成";
      loadingGraph("服务暂不可用", text, false);
    }
    return false;
  }
}
async function initialize() {
  try {
    const response = await fetch("/playground/bootstrap", {
      cache: "no-store",
    });
    if (!response.ok) throw new Error("无法读取服务配置。");
    bootstrap = await response.json();
    if(bootstrap.data_scope === "pump-only") {
      for(const id of ["family", "search-family", "sources-family", "search-references"])
        $(id)?.closest("label")?.setAttribute("hidden", "");
      $("search-asset").placeholder = "如 BC-P-101";
    }
    client.configureBootstrap(bootstrap);
    retrievalOptions=mountRetrievalOptions($("search-options"),{bootstrap,onChange:resetSearchScope});
    const personas = bootstrap.personas || [];
    if (!personas.length) throw new Error("当前服务尚未提供可用身份。");
    let defaultPersona;
    workspacePicker=mountWorkspacePicker({bootstrap,client,onChange:async id=>{ $("persona").value=id;await changePersona(); }});
    if(workspacePicker) defaultPersona=personas.find(person=>person.id===$("persona").value);
    else {
    clear($("persona"));
    const tenantGroups = new Map();
    for (const persona of personas) {
      if (!tenantGroups.has(persona.tenant_id)) {
        const group = element("optgroup");
        group.label = ({ "industrial-schneider-demo": "工业知识库", "demo-a": "一号泵站", "demo-b": "二号泵站" })[persona.tenant_id] || persona.tenant_id;
        tenantGroups.set(persona.tenant_id, group);
        $("persona").append(group);
      }
      tenantGroups.get(persona.tenant_id).append(new Option(persona.label, persona.id));
    }
    const fullScopes = ["ontology:write", "ontology:publish", "knowledge:import", "knowledge:construct", "knowledge:review", "knowledge:publish", "knowledge:quality", "knowledge:lifecycle"];
    defaultPersona = personas.find(item => item.id === bootstrap.industrial?.default_persona_id)
      || personas.find(item => fullScopes.every(scope => item.scopes?.includes(scope))) || personas[0];
    $("persona").value = defaultPersona.id;
    $("persona").disabled = false;
    }
    for (const id of ["family", "search-family", "sources-family"]) $(id).value = "";
    document.querySelectorAll("[data-view]").forEach(item => item.classList.toggle("selected", item.dataset.view === view));
    graph = new IndustrialGraph($("graph-canvas"), {
      onSelect: selected => void selectGraph(selected),
      onViewDetails: viewSelectedDetails,
    });
    for (const kind of ["graph", "search"]) {
      const note = element("div", "source-note");
      note.id = `${kind}-document-scope`;
      note.hidden = true;
      if (kind === "graph") document.querySelector("#panel-explore .graph-workspace").insertAdjacentElement("beforebegin", note);
      else $("search-form").append(note);
    }
    const copy = button("复制检索上下文", copyContexts);
    copy.id = "copy-contexts";
    copy.disabled = true;
    $("search-summary").insertAdjacentElement("afterend", copy);
    sourceCatalog = new SourceCatalog({
      client: {
        get epoch() { return client.epoch; },
        request(path, body) {
          if (path === "/v1/industrial/sources:query" && !isIndustrialIdentity())
            return Promise.resolve({ sources: [], has_more: false });
          return client.request(path, body);
        },
      },
      list: $("sources-list"), summary: $("sources-summary"), onSelect: selectSource,
    });
    construction = await mountGovernance($("governance-host"), {
      client, bootstrap, onNavigate: name => navigate(name, true),
      onExplore: async entityId => {
        graphSource=null;updateSourceScope("graph");
        $("family").value="";$("asset").value="";view="all";$("graph-relation-filter").value="";
        document.querySelectorAll("[data-view]").forEach(item=>item.classList.toggle("selected",item.dataset.view===view));
        navigate("explore");await loadGraph({seed_entity_ids:[entityId]});
      },
      onSource: citation => sourceCatalog.open({document_id:citation.document_id,version_id:citation.version_id,title:citation.document_title},citation.ordinal||0),
      onPublished: async (entityIds = []) => {
        const identity = client.epoch;
        graphSource = null;
        searchSource = null;
        updateSourceScope("graph");
        updateSourceScope("search");
        $("family").value = "";
        $("asset").value = "";
        $("trust").value = "PUBLISHED_SECONDARY_INCLUSIVE";
        $("search-family").value = "";
        $("search-asset").value = "";
        updateReferenceControl();
        resetSearchScope();
        view = "all";
        $("graph-relation-filter").value = "";
        document.querySelectorAll("[data-view]").forEach(item => item.classList.toggle("selected", item.dataset.view === view));
        resetGraph();
        await loadSources({ populateAssets: true });
        if (identity !== client.epoch) return;
        await loadGraph({ seed_entity_ids: entityIds.slice(0, 8) });
      },
    });
    await changePersona();
  } catch (error) {
    message(safeError(error), true);
    $("service-status").textContent = "连接未完成";
    loadingGraph("工业工作台尚未就绪", safeError(error), false);
  }
}
document
  .querySelectorAll("[data-panel]")
  .forEach((item) =>
    item.addEventListener("click", () => navigate(item.dataset.panel)),
  );
document.querySelectorAll("[data-view]").forEach((item) =>
  item.addEventListener("click", () => {
    if (view === item.dataset.view) return;
    view = item.dataset.view;
    document
      .querySelectorAll("[data-view]")
      .forEach((b) => b.classList.toggle("selected", b === item));
    void loadGraph();
  }),
);
$("graph-relation-filter").addEventListener("change", () => void loadGraph());
$("persona").addEventListener("change", () => void changePersona());
$("refresh-session").addEventListener("click", () => {void (workspacePicker?workspacePicker.refresh():changePersona()).catch(error=>message(safeError(error),true));});
$("reload-graph").addEventListener("click", () => {
  message();
  void loadGraph();
  void loadSources({ populateAssets: true });
});
$("family").addEventListener("change", async () => {
  graphSource = null;
  searchSource = null;
  updateSourceScope("graph");
  updateSourceScope("search");
  resetGraph();
  loadingGraph();
  const identity = client.epoch,
    family = $("family").value;
  clear($("asset")).append(new Option("当前产品族的可见设备", ""));
  $("search-family").value = $("family").value;
  resetSearchScope();
  await loadSources({ populateAssets: true, autoSelectAsset: true });
  if (identity !== client.epoch || family !== $("family").value) return;
  updateReferenceControl();
  await loadGraph();
});
$("asset").addEventListener("change", () => {
  graphSource = null;
  searchSource = null;
  updateSourceScope("graph");
  updateSourceScope("search");
  $("search-asset").value = $("asset").value
    ? assetLabel($("asset").value)
    : "";
  updateReferenceControl();
  resetSearchScope();
  void loadGraph();
});
$("trust").addEventListener("change", () => void loadGraph());
$("fit-graph").addEventListener("click", () => graph?.fit());
$("zoom-in").addEventListener("click", () => graph?.zoom(1.2));
$("zoom-out").addEventListener("click", () => graph?.zoom(1 / 1.2));
$("export-graph").addEventListener("click", () => {
  if (!graph?.cy.nodes().length) return;
  const blob = graph.exportPNG(),
    url = URL.createObjectURL(blob);
  const link = element("a");
  link.href = url;
  link.download = `industrial-${view}.png`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
$("next-page").addEventListener("click", event => {
  if (page?.page.next_cursor)
    void loadGraph({
      ...pageQuery,
      view_token: page.view_token,
      cursor: page.page.next_cursor,
    }, event.currentTarget);
});
$("search-family").addEventListener("change", () => {
  searchSource = null;
  updateSourceScope("search");
  $("search-asset").value = "";
  updateReferenceControl();
  resetSearchScope();
});
for (const id of ["search-asset", "search-references"])
  $(id).addEventListener("change", () => { updateReferenceControl(); resetSearchScope(); });
$("search-form").addEventListener("submit", search);
$("question").addEventListener("input", resetSearchScope);
document.querySelectorAll("[data-question]").forEach((item) =>
  item.addEventListener("click", () => {
    searchSource = null;
    updateSourceScope("search");
    resetSearchScope();
    $("question").value = item.dataset.question;
    if (item.dataset.family) $("search-family").value = item.dataset.family;
    $("search-asset").value = item.dataset.asset || "";
    updateReferenceControl();
    $("question").focus();
  }),
);
$("refresh-sources").addEventListener("click", () => void loadSources());
$("sources-family").addEventListener("change", () => void loadSources());
void initialize();
