import {
  $,
  element,
  button,
  clear,
  tag,
  metadata,
  sourceTag,
  authorityTag,
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
} from "./core.mjs";
import { IndustrialGraph } from "./graph.mjs";
import { TYPE_COLORS, VIEWS, samePin } from "./graph-model.mjs";
import { ConstructionWorkbench } from "./construction.mjs";

const client = new WorkbenchClient();
let graph,
  construction,
  bootstrap,
  page = null,
  pageQuery = null,
  view = "composition",
  graphEpoch = 0,
  selectionEpoch = 0,
  searchEpoch = 0,
  sourcesEpoch = 0,
  searchBusy = false,
  panel = "explore",
  sourceRows = [];
const panelNames = {
  explore: "图谱探索",
  search: "证据检索",
  sources: "来源资料",
  build: "知识构建",
};
const initialInspector = $("inspector-content").cloneNode(true);

function resetInspector() {
  selectionEpoch++;
  $("inspector-content").replaceChildren(
    ...[...initialInspector.childNodes].map((n) => n.cloneNode(true)),
  );
}
function loadingGraph(
  text = "正在载入有来源的知识关系",
  detail = "构建当前身份可见的图谱视图",
  busy = true,
) {
  const placeholder = $("graph-placeholder");
  placeholder.hidden = false;
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
  $("graph-version").textContent = "连接中";
  $("graph-timing").textContent = "";
  $("next-page").hidden = true;
}
function updateStats(result) {
  $("node-count-label").textContent =
    view === "ontology" ? "声明的节点类型" : "当前可见节点";
  $("edge-count-label").textContent =
    view === "ontology" ? "允许的类型连接" : "有证据的关系";
  document.querySelector(".graph-key").hidden = view === "ontology";
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
  const countNote = view === "ontology" ? "本体类型与规则" : "知识发布";
  $("graph-version").textContent =
    `${countNote} · ${page.pin.publication_generation || "—"}`;
  $("canvas-caption").textContent =
    view === "ontology"
      ? "本体模型 · 展示允许的类型与关系，不代表实例事实"
      : `${VIEWS[view].label} · 滚轮缩放 · 拖动节点 · 点击查看依据`;
}
function graphBody(extra = {}) {
  return {
    industrial_scope: graphScope($("family").value, $("asset").value),
    trust_policy: $("trust").value,
    entity_types: [],
    predicates: VIEWS[view].predicates,
    name_query: null,
    seed_entity_ids: [],
    direction: "both",
    hops: 1,
    page_size: 100,
    ...extra,
  };
}
async function loadGraph(extra = {}) {
  const epoch = ++graphEpoch;
  selectionEpoch++;
  const identity = client.epoch;
  graph?.clear();
  resetInspector();
  page = null;
  $("next-page").hidden = true;
  loadingGraph();
  const started = performance.now();
  const query = graphBody(extra);
  try {
    const result = await client.request("/v1/knowledge/graph:query", query);
    if (epoch !== graphEpoch || identity !== client.epoch) return;
    page = result;
    pageQuery = query;
    const counts = graph.setPage(page, view);
    updateStats(counts);
    $("graph-timing").textContent =
      `${Math.round(performance.now() - started)} ms`;
    $("next-page").hidden = !page.page.has_more || view === "ontology";
    if (counts.nodes) {
      $("graph-placeholder").hidden = true;
    } else
      loadingGraph(
        "当前范围没有可见关系",
        "可切换产品族、事实范围或查看身份。",
        false,
      );
    if (extra.seed_entity_ids?.length) toast("已显示所选节点周围的授权关系。");
  } catch (error) {
    if (epoch !== graphEpoch || identity !== client.epoch) return;
    const text = safeError(error);
    if (text) {
      loadingGraph("图谱暂未载入", text, false);
      message(text, true);
    }
  }
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
    renderSchema(selected);
    return;
  }
  const current = page,
    epoch = graphEpoch,
    identity = client.epoch,
    selection = ++selectionEpoch;
  if (!current) return;
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
  for (const authority of entity?.authority_levels || [
    assertion?.authority_level,
  ])
    if (authority) tags.append(authorityTag(authority));
  if (assertion) {
    tags.append(tag(ORIGIN_LABELS[assertion.origin] || assertion.origin));
    if (assertion.source_kind) tags.append(sourceTag(assertion.source_kind));
  }
  target.append(tags);
  if (entity) {
    target.append(element("div", "record-key", entity.canonical_key));
    const expand = button("展开一跳关系 →", () =>
      loadGraph({
        view_token: current.view_token,
        seed_entity_ids: [entity.entity_id],
        hops: 1,
      }),
    );
    const section = element("div", "detail-section");
    section.append(expand);
    target.append(section);
    const literals = current.literals.filter(
      (item) => item.subject === entity.entity_id,
    );
    if (literals.length) {
      const values = element("div", "detail-section");
      values.append(element("h3", "", "有来源的属性"));
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
    : [assertion.revision_id];
  const evidenceArea = element("div", "detail-section");
  evidenceArea.append(
    element("h3", "", "来源与事实依据"),
    element("p", "", "正在核对来源…"),
  );
  target.append(evidenceArea);
  try {
    const data = await client.request("/v1/knowledge/graph:evidence", {
      view_token: current.view_token,
      revision_ids: revisions.slice(0, 10),
    });
    if (
      epoch !== graphEpoch ||
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
    if (revisions.length > 10)
      evidenceArea.append(
        element(
          "p",
          "muted",
          `显示前 10 条可见来源提及，共 ${revisions.length} 条。`,
        ),
      );
  } catch (error) {
    if (
      epoch !== graphEpoch ||
      identity !== client.epoch ||
      selection !== selectionEpoch
    )
      return;
    const text = safeError(error);
    if (text) {
      clear(evidenceArea).append(element("p", "danger-text", text));
    }
  }
}
function renderEvidence(item) {
  const node = element("article", "evidence-detail");
  const evidence = item.evidence,
    citation = evidence.citation;
  const tags = element("div", "tag-row");
  tags.append(
    sourceTag(item.source_kind),
    tag(STATUS_LABELS[item.status] || item.status),
  );
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

async function loadSources({
  populateAssets = false,
  autoSelectAsset = false,
} = {}) {
  const epoch = ++sourcesEpoch,
    identity = client.epoch;
  $("sources-summary").textContent = "正在读取当前身份可见的来源…";
  try {
    const result = await client.request("/v1/industrial/sources:query", {
      family:
        (populateAssets ? $("family").value : $("sources-family").value) ||
        null,
      asset_keys: [],
      limit: 100,
    });
    if (epoch !== sourcesEpoch || identity !== client.epoch) return;
    sourceRows = result.sources;
    const list = clear($("sources-list"));
    $("sources-summary").textContent =
      `当前范围可见 ${sourceRows.length} 份资料${result.has_more ? " · 还有更多授权资料，请缩小产品范围" : ""} · 来源资料不等同于已发布图谱事实`;
    if (!sourceRows.length) list.append(empty("当前产品族没有可见资料。"));
    for (const source of sourceRows) {
      const card = element("article", "source-card");
      card.append(
        element("div", "source-mark", "▤"),
        sourceTag(source.source_kind),
        element("h3", "", source.title),
        element(
          "p",
          "",
          source.family === "canalis-kt"
            ? "Canalis KT · 母线槽"
            : "EvoPacT HVX · 真空断路器",
        ),
        metadata([
          `${source.chunk_count} 个来源片段`,
          source.asset_keys.length
            ? source.asset_keys.map(assetLabel).join("、")
            : "产品族参考资料",
          dateLabel(source.published_at),
        ]),
        button("查看来源片段 →", () => openSource(source), "text-button"),
      );
      list.append(card);
    }
    if (populateAssets) {
      const selected = $("asset").value;
      const keys = [
        ...new Set(sourceRows.flatMap((row) => row.asset_keys)),
      ].sort();
      clear($("asset")).append(new Option("当前产品族的可见设备", ""));
      for (const key of keys)
        $("asset").append(new Option(assetLabel(key), key));
      if (keys.includes(selected)) $("asset").value = selected;
      else if (autoSelectAsset && keys.length) $("asset").value = keys[0];
      if (autoSelectAsset)
        $("search-asset").value = $("asset").value
          ? assetLabel($("asset").value)
          : "";
    }
  } catch (error) {
    if (epoch !== sourcesEpoch || identity !== client.epoch) return;
    const text = safeError(error);
    if (text) {
      $("sources-summary").textContent = text;
      clear($("sources-list")).append(empty(text));
    }
  }
}
async function openSource(source) {
  const identity = client.epoch;
  const modal = document.createElement("dialog");
  modal.className = "source-dialog";
  const title = element("div", "dialog-heading");
  title.append(
    element("h2", "", source.title),
    button("关闭", () => modal.close(), "button secondary"),
  );
  modal.append(title, element("p", "muted", "正在读取原始来源片段…"));
  document.body.append(modal);
  modal.addEventListener("close", () => modal.remove());
  modal.showModal();
  try {
    const result = await client.request("/v1/industrial/sources:chunk", {
      chunk_id: source.first_chunk_id,
    });
    if (identity !== client.epoch) {
      modal.close();
      return;
    }
    modal.lastChild.remove();
    renderSourceChunk(modal, result, source);
  } catch (error) {
    if (identity !== client.epoch) {
      modal.close();
      return;
    }
    const text = safeError(error);
    if (text) modal.append(element("p", "danger-text", text));
  }
}
function renderSourceChunk(modal, envelope, source) {
  const old = modal.querySelector(".source-dialog-content");
  old?.remove();
  const content = element("div", "source-dialog-content");
  const result = envelope.chunk;
  if (!result) {
    content.append(empty("此片段当前不可见，或资料版本已变化。"));
    modal.append(content);
    return;
  }
  if (result.source.version_id !== source.version_id) {
    content.append(empty("资料版本已变化，请重新打开来源。"));
    modal.append(content);
    return;
  }
  content.append(
    metadata([
      result.section,
      result.page_number ? `原文第 ${result.page_number} 页` : null,
      result.provenance?.source?.document_reference,
      result.provenance?.source?.embedded_revision,
      `片段 ${result.ordinal + 1} / ${result.source.chunk_count}`,
      `字符 ${result.char_start}–${result.char_end}`,
    ]),
    element("pre", "source-quote", result.text),
  );
  const link =
    sourceLink(result.provenance?.source?.portal_url) ||
    sourceLink(result.source.canonical_uri);
  if (link) content.append(link);
  const nav = element("div", "form-footer");
  for (const [label, key] of [
    ["← 上一片段", "previous_chunk_id"],
    ["下一片段 →", "next_chunk_id"],
  ])
    if (result[key]) {
      nav.append(
        button(label, async () => {
          const identity = client.epoch;
          try {
            const next = await client.request("/v1/industrial/sources:chunk", {
              chunk_id: result[key],
            });
            if (identity !== client.epoch || !modal.open) return;
            renderSourceChunk(modal, next, source);
          } catch (error) {
            const text = safeError(error);
            if (text && identity === client.epoch) toast(text);
          }
        }),
      );
    }
  content.append(nav);
  modal.append(content);
}
function resetSearchScope() {
  searchEpoch++;
  clear($("search-results"));
  $("search-summary").textContent = searchBusy
    ? "上一个检索仍在处理；范围已更新，完成后可重新检索。"
    : "";
}
function navigate(name) {
  panel = name;
  for (const [id, label] of Object.entries(panelNames)) {
    const active = id === name;
    $(`panel-${id}`).hidden = !active;
    $(`panel-${id}`).classList.toggle("active", active);
    document
      .querySelector(`[data-panel="${id}"]`)
      .classList.toggle("active", active);
    if (active) $("breadcrumb-current").textContent = label;
  }
  if (name === "explore") requestAnimationFrame(() => graph?.fit());
  if (name === "sources") void loadSources();
  if (name === "build") construction?.refreshCapabilities();
}
async function search(event) {
  event.preventDefault();
  if (searchBusy) return;
  const identity = client.epoch,
    epoch = ++searchEpoch;
  const query = $("question").value.trim();
  if (!query) return;
  searchBusy = true;
  const submit = $("search-submit");
  submit.disabled = true;
  submit.classList.add("busy");
  $("search-summary").textContent = "正在检索、核对适用范围并排序证据…";
  clear($("search-results"));
  const start = performance.now();
  try {
    const scope = graphScope($("search-family").value, $("search-asset").value);
    scope.include_references = $("search-references").checked;
    const result = await client.request("/v1/retrieval", {
      query_text: query,
      industrial_scope: scope,
      include_graph: false,
    });
    if (identity !== client.epoch || epoch !== searchEpoch) return;
    const rows = result.chunks || [];
    $("search-summary").textContent =
      `${rows.length} 条来源片段 · ${(performance.now() - start) / 1000 < 1 ? Math.round(performance.now() - start) + " ms" : ((performance.now() - start) / 1000).toFixed(1) + " s"} · ${result.trace?.reranking ? "已进行证据排序" : "来源检索"}`;
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
      const link = sourceLink(citation.canonical_uri);
      if (link) card.append(link);
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
async function changePersona() {
  message();
  resetGraph();
  sourceRows = [];
  sourcesEpoch++;
  searchEpoch++;
  searchBusy = false;
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
  loadingGraph();
  try {
    await client.selectPersona($("persona").value);
    $("service-status").textContent = "本地服务已连接";
    construction?.refreshCapabilities();
    const identity = client.epoch;
    await loadSources({ populateAssets: true, autoSelectAsset: true });
    if (identity !== client.epoch) return;
    await loadGraph();
    if (panel === "sources") await loadSources();
  } catch (error) {
    const text = safeError(error);
    if (text) {
      message(text, true);
      $("service-status").textContent = "连接未完成";
      loadingGraph("服务暂不可用", text, false);
    }
  }
}
async function initialize() {
  try {
    const response = await fetch("/playground/bootstrap", {
      cache: "no-store",
    });
    if (!response.ok) throw new Error("无法读取服务配置。");
    bootstrap = await response.json();
    const personas = (bootstrap.personas || []).filter(
      (p) => p.tenant_id === "industrial-schneider-demo",
    );
    if (!personas.length)
      throw new Error(
        "当前服务尚未启用工业工作台，请使用启用工业库的启动命令。",
      );
    clear($("persona"));
    for (const persona of personas)
      $("persona").append(new Option(persona.label, persona.id));
    $("persona").value =
      bootstrap.industrial?.default_persona_id || "persona-11";
    $("persona").disabled = false;
    graph = new IndustrialGraph($("graph-canvas"), {
      onSelect: (selected) => void selectGraph(selected),
    });
    construction = new ConstructionWorkbench(client, {
      onPublished: async (entityIds) => {
        const identity = client.epoch;
        $("family").value = "";
        $("asset").value = "";
        $("trust").value = "PUBLISHED_SECONDARY_INCLUSIVE";
        $("search-family").value = "";
        $("search-asset").value = "";
        resetSearchScope();
        view = "all";
        document
          .querySelectorAll("[data-view]")
          .forEach((item) =>
            item.classList.toggle("selected", item.dataset.view === view),
          );
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
$("persona").addEventListener("change", () => void changePersona());
$("refresh-session").addEventListener("click", () => void changePersona());
$("reload-graph").addEventListener("click", () => {
  message();
  void loadGraph();
  void loadSources({ populateAssets: true });
});
$("family").addEventListener("change", async () => {
  resetGraph();
  loadingGraph();
  const identity = client.epoch,
    family = $("family").value;
  clear($("asset")).append(new Option("当前产品族的可见设备", ""));
  $("search-family").value = $("family").value;
  resetSearchScope();
  await loadSources({ populateAssets: true, autoSelectAsset: true });
  if (identity !== client.epoch || family !== $("family").value) return;
  await loadGraph();
});
$("asset").addEventListener("change", () => {
  $("search-asset").value = $("asset").value
    ? assetLabel($("asset").value)
    : "";
  resetSearchScope();
  void loadGraph();
});
$("trust").addEventListener("change", () => void loadGraph());
$("fit-graph").addEventListener("click", () => graph?.fit());
$("zoom-in").addEventListener("click", () => graph?.zoom(1.2));
$("zoom-out").addEventListener("click", () => graph?.zoom(1 / 1.2));
$("export-graph").addEventListener("click", () => {
  if (!page?.nodes.length) return;
  const blob = graph.exportPNG(),
    url = URL.createObjectURL(blob);
  const link = element("a");
  link.href = url;
  link.download = `industrial-${view}.png`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
$("next-page").addEventListener("click", () => {
  if (page?.page.next_cursor)
    void loadGraph({
      ...pageQuery,
      view_token: page.view_token,
      cursor: page.page.next_cursor,
    });
});
$("search-family").addEventListener("change", () => {
  $("search-asset").value = "";
  resetSearchScope();
});
for (const id of ["search-asset", "search-references"])
  $(id).addEventListener("change", resetSearchScope);
$("search-form").addEventListener("submit", search);
$("question").addEventListener("input", resetSearchScope);
document.querySelectorAll("[data-question]").forEach((item) =>
  item.addEventListener("click", () => {
    resetSearchScope();
    $("question").value = item.dataset.question;
    if (item.dataset.family) $("search-family").value = item.dataset.family;
    $("search-asset").value = item.dataset.asset || "";
    $("question").focus();
  }),
);
$("refresh-sources").addEventListener("click", () => void loadSources());
$("sources-family").addEventListener("change", () => void loadSources());
void initialize();
