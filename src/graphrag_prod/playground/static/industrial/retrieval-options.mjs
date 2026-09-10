import { element, button } from "./core.mjs";

// Keep these bounds aligned with api.contracts.RetrievalLimitsRequest.
export const RETRIEVAL_LIMIT_FIELDS = Object.freeze([
  ["top_k", "返回片段数", 1, 20, 5],
  ["vector_recall_k", "向量召回数", 1, 100, 20],
  ["bm25_recall_k", "关键词召回数", 1, 100, 20],
  ["bm25_scan_k", "关键词扫描数", 1, 500, 100],
  ["seed_k", "图扩展起点数", 1, 20, 5],
  ["graph_entities_per_seed", "每个起点的实体数", 1, 50, 20],
  ["graph_edges_per_seed", "每个起点的关系数", 1, 200, 100],
  ["graph_candidates_per_seed", "每个起点的候选数", 1, 50, 20],
  ["candidate_limit", "候选片段上限", 1, 200, 100],
  ["anchor_k", "上下文锚点数", 1, 20, 3],
  ["adjacent_window", "相邻片段窗口", 0, 3, 1],
  ["max_context_chars", "上下文字符上限", 256, 30000, 12000],
  ["rrf_rank_constant", "RRF 排名常数", 1, 1000, 60],
  ["minimum_vector_score", "最低向量分数", 0, 1, 0, "any"],
  ["minimum_bm25_score", "最低关键词分数", 0, 1000000, 0, "any"],
  ["minimum_rrf_channels", "最少命中检索通道", 1, 2, 1],
].map(([key, label, min, max, fallback, step = 1]) =>
  Object.freeze({ key, label, min, max, fallback, step })));

const TRUST_POLICIES = new Set(["PUBLISHED_SECONDARY_INCLUSIVE", "AUTHORITATIVE_ONLY"]);

export function rerankingLabel(trace) {
  if (!trace) return "排序信息未返回";
  if (trace.reranking?.status === "RERANKED") return "已完成模型重排";
  if (trace.reranking?.status === "SKIPPED_EMPTY") return "无候选，已跳过模型重排";
  if (trace.reranking) return "重排状态未知";
  return "RRF 融合排序 · 未启用模型重排";
}

const list = value => Array.isArray(value) ? value : [];
const number = value => typeof value === "number" && Number.isFinite(value) ? String(value) : "未返回";
const score = value => typeof value === "number" && Number.isFinite(value) ? value.toFixed(6) : "—";
const hitMap = rows => new Map(list(rows).map(row => [row.chunk_id, row]));
const count = rows => Array.isArray(rows) ? `${new Set(rows.map(row => row.chunk_id)).size} 个片段` : "未返回记录";
const REJECTION_LABELS = {
  insufficient_rrf_channels: "命中通道不足", final_authorization_or_version_check: "权限或版本复核未通过",
  duplicate_content: "重复内容", rerank_candidate_limit: "未进入模型重排候选集",
  chunk_limit: "返回片段数达到上限", character_budget: "上下文字符预算不足", missing_or_empty: "原文缺失或为空",
};

function explanation(parent, title, text) {
  const item = element("div", "retrieval-explanation");
  item.append(element("h4", "", title), element("p", "muted", text));
  parent.append(item);
}

function methodGuide() {
  const guide = element("details", "retrieval-method-guide");
  guide.append(element("summary", "", "检索引擎与评分规则"));
  explanation(guide, "1 · 向量与关键词召回", "Neo4j 向量余弦相似度查找语义相关原文；Lucene / BM25 全文检索匹配关键词。两种分数的尺度不同，不直接相加。");
  explanation(guide, "2 · 图谱补充候选", "先用 RRF 融合召回排名选取起点，再沿共享实体查找相关片段。Resource Allocation 按共享实体的连接度倒数求和，连接越普遍，贡献越小。图扩展分数只用于候选扩展，不直接加进最终 RRF 分数。");
  explanation(guide, "3 · 融合与模型重排", "对候选重新计算向量排名，与关键词排名做 RRF 融合：分数 = Σ 1 /（k + 通道名次），名次从 1 开始。若服务启用模型重排，按模型返回的顺序选取候选；模型分数不与 RRF 相加，部分模型只返回顺序。");
  explanation(guide, "4 · 组织可引用上下文", "先选核心片段，再补充相邻原文，最后按排名补足；受去重、片段数和字符上限约束。因此显示序号是阅读顺序，不一定是相关性名次。所有分数均不代表事实可信度或回答正确率。");
  return guide;
}

function renderProcess(result, host) {
  const trace = result.trace;
  const diagnostics = element("details", "retrieval-diagnostics");
  diagnostics.append(element("summary", "", "检索过程与评分说明"));
  if (!trace) {
    diagnostics.append(element("p", "muted", "本次响应未提供检索过程，无法确认引擎执行情况与评分。"));
  } else {
    const stages = element("div", "retrieval-stages");
    explanation(stages, "语义召回", `向量余弦相似度 · ${count(trace.vector_recall)}`);
    explanation(stages, "关键词召回", `Lucene / BM25 · ${count(trace.bm25_recall)}`);
    explanation(stages, "图谱扩展", `Resource Allocation · ${count(trace.graph_expansion)}（按片段去重，可能与召回重叠）`);
    explanation(stages, "融合排序", `RRF · ${count(trace.final_ranking)} · k = ${number(trace.limits?.rrf_rank_constant)}`);
    explanation(stages, "模型重排", `${rerankingLabel(trace)}${trace.reranking?.response ? ` · ${trace.reranking.response.model} · ${number(trace.reranking.response.candidate_count)} 个候选` : ""}`);
    explanation(stages, "返回上下文", `${list(result.chunks).length} 个片段 · ${number(trace.context_chars)} 字符`);
    diagnostics.append(stages);
    diagnostics.append(element("p", "muted", "各阶段片段可能重叠，数量不能相加；0 个片段表示本阶段没有返回命中，不能据此判断引擎未启用。"));
    const limits = trace.limits || {};
    diagnostics.append(element("p", "retrieval-effective-limits", `本次实际门槛：向量 ≥ ${number(limits.minimum_vector_score)}；BM25 ≥ ${number(limits.minimum_bm25_score)}；至少命中 ${number(limits.minimum_rrf_channels)} 个融合通道。最多返回 ${number(limits.top_k)} 个片段 / ${number(limits.max_context_chars)} 字符。`));
    const rejected = new Map();
    for (const decision of list(trace.decisions).filter(row => row.decision === "rejected")) {
      const ids = rejected.get(decision.reason) || new Set();
      ids.add(decision.chunk_id);
      rejected.set(decision.reason, ids);
    }
    if (rejected.size) {
      const reasons = element("ul", "retrieval-rejections");
      for (const [reason, ids] of rejected) reasons.append(element("li", "", `${REJECTION_LABELS[reason] || "其他筛选原因（详见调试数据）"}：${ids.size} 个片段`));
      diagnostics.append(element("h4", "", "未选入上下文的原因（同一片段可能涉及多项）"), reasons);
    }
    if (rejected.has("character_budget") || rejected.has("chunk_limit") || rejected.has("rerank_candidate_limit")) {
      host.append(element("p", "retrieval-limit-notice", "本次有候选未进入上下文或模型重排范围。结果受预算限制，不代表知识库内的全部相关证据；展开检索过程可查看原因。"));
    }
    const scoring = element("details", "retrieval-scoring");
    scoring.append(element("summary", "", "返回片段为什么入选？"));
    scoring.append(element("p", "muted", "序号对应上方证据卡片。向量与 BM25 列显示候选融合时的分数和名次；RRF 为重排前融合分数。— 表示没有记录或模型未提供分数，不按 0 处理。相邻原文用于补全上下文，不保证按相关性排列。"));
    const table = element("table", "retrieval-score-table");
    table.append(element("caption", "", "本次返回片段的评分与入选用途"));
    const head = element("tr");
    for (const title of ["片段 / 用途", "向量", "BM25", "RRF", "模型重排"]) {
      const cell = element("th", "", title); cell.scope = "col"; head.append(cell);
    }
    const thead = element("thead"); thead.append(head); table.append(thead);
    const tbody = element("tbody");
    const vectors = hitMap(trace.candidate_vector_ranking), keywords = hitMap(trace.bm25_recall), fused = hitMap(trace.final_ranking);
    const reranked = trace.reranking?.status === "RERANKED";
    const scores = hitMap(reranked ? trace.reranking?.response?.scores : []);
    const positions = new Map(list(reranked ? trace.reranking?.ranked_chunk_ids : []).map((id, index) => [id, index + 1]));
    const channelScore = hit => hit ? `${score(hit.score)} · 第 ${number(hit.rank)} 名` : "—";
    list(result.chunks).forEach((chunk, index) => {
      const id = chunk.citation?.chunk_id, row = element("tr");
      const role = ({ anchor: "核心片段", adjacent: "相邻补全", ranked: "排名补足" })[chunk.role] || "用途未返回";
      const title = chunk.citation?.document_title || "来源标题未返回";
      const values = [`${index + 1} · ${role}\n${title}`, channelScore(vectors.get(id)), channelScore(keywords.get(id)),
        score(fused.get(id)?.score), positions.has(id) ? `第 ${positions.get(id)} 名 · ${score(scores.get(id)?.score)}` : "—"];
      for (const value of values) row.append(element("td", "", value));
      tbody.append(row);
    });
    table.append(tbody);
    const scroll = element("div", "retrieval-score-scroll"); scroll.tabIndex = 0; scroll.append(table);
    scoring.append(scroll);
    if (!list(result.chunks).length) scoring.append(element("p", "muted", "本次没有返回片段。"));
    diagnostics.append(scoring, methodGuide());
  }
  const debug = element("details", "retrieval-debug");
  debug.append(element("summary", "", "调试数据 · 原始响应 JSON"));
  debug.append(element("pre", "raw-output", JSON.stringify(result, null, 2)));
  diagnostics.append(debug);
  host.append(diagnostics);
}

function parseIds(value, label) {
  const ids = [...new Set(String(value || "").split(/[\s,，]+/u).filter(Boolean))];
  if (ids.length > 100) throw new Error(`${label}最多允许 100 个 ID`);
  if (ids.some((id) => Array.from(id).length > 256 || /[\x00-\x20\x7f]/u.test(id))) {
    throw new Error(`${label}中的 ID 不可含控制字符，且不得超过 256 个字符`);
  }
  return ids;
}

/** Build only the retrieval endpoint's supported, bounded request options. */
export function parseRetrievalOptions(values = {}) {
  const documentIds = parseIds(values.documentIds, "文档过滤");
  const versionIds = parseIds(values.versionIds, "版本过滤");
  if (documentIds.length + versionIds.length > 100) {
    throw new Error("文档与版本过滤合计最多允许 100 个 ID");
  }
  const cutoffValue = String(values.publishedBefore || "").trim();
  const cutoff = cutoffValue ? new Date(cutoffValue) : null;
  if (cutoff && Number.isNaN(cutoff.getTime())) throw new Error("发布截止时间无效");
  const trust = values.graphTrustPolicy || "PUBLISHED_SECONDARY_INCLUSIVE";
  if (!TRUST_POLICIES.has(trust)) throw new Error("请选择有效的图谱事实范围");
  const result = { include_graph: values.includeGraph === true, graph_trust_policy: trust };
  if (documentIds.length || versionIds.length || cutoff) {
    result.version_filter = {
      document_ids: documentIds,
      version_ids: versionIds,
      published_at_or_before: cutoff ? cutoff.toISOString() : null,
    };
  }
  // Omitting limits lets the server select the correct generic / industrial budget.
  if (!values.customLimits) return result;
  const limits = {};
  for (const field of RETRIEVAL_LIMIT_FIELDS) {
    const raw = values.limits?.[field.key];
    if (raw === undefined || raw === null || String(raw).trim() === "") {
      throw new Error(`${field.label}不能为空`);
    }
    const value = typeof raw === "boolean" ? NaN : Number(raw);
    if (!Number.isFinite(value) || value < field.min || value > field.max ||
        (field.step === 1 && !Number.isInteger(value))) {
      throw new Error(`${field.label}须在 ${field.min}–${field.max} 之间${field.step === 1 ? "，且为整数" : ""}`);
    }
    limits[field.key] = value;
  }
  if (typeof values.limits?.deduplicate_content !== "boolean") {
    throw new Error("请明确是否去除重复内容");
  }
  limits.deduplicate_content = values.limits.deduplicate_content;
  if (limits.bm25_scan_k < limits.bm25_recall_k) throw new Error("关键词扫描数不能小于关键词召回数");
  if (limits.seed_k > limits.candidate_limit) throw new Error("图扩展起点数不能超过候选片段上限");
  if (limits.anchor_k > limits.top_k) throw new Error("上下文锚点数不能超过返回片段数");
  result.limits = limits;
  return result;
}

/** Native advanced options; callers own query submission, identity, and result clearing. */
export function mountRetrievalOptions(container, { bootstrap = {}, onChange = () => {} } = {}) {
  const root = element("details", "retrieval-options");
  root.append(element("summary", "", "检索选项"));
  const body = element("div", "retrieval-options-body");
  body.append(element("p", "muted", "通常保留默认即可。检索只在当前知识库、身份权限与所选资料范围内进行；执行后可查看本次引擎、评分和筛选原因。"), methodGuide());
  const filters = element("div", "form-grid retrieval-filter-grid");
  const diagnosticsHosts = new Set();
  function changed() {
    for (const host of diagnosticsHosts) host.replaceChildren();
    onChange();
  }
  function field(parent, label, input) {
    const wrapper = element("label", "", label);
    wrapper.append(input);
    parent.append(wrapper);
    input.addEventListener(input.type === "checkbox" || input.tagName === "SELECT" ? "change" : "input", changed);
    return input;
  }
  function input(type, name) {
    const node = element("input");
    node.type = type;
    node.name = name;
    return node;
  }
  const documentIds = field(filters, "文档 ID（空格或逗号分隔）", input("text", "documentIds"));
  const versionIds = field(filters, "文档版本 ID（空格或逗号分隔）", input("text", "versionIds"));
  documentIds.maxLength = versionIds.maxLength = 25700;
  const publishedBefore = field(filters, "发布截止时间（本地时间）", input("datetime-local", "publishedBefore"));
  const graph = element("div", "retrieval-graph-options");
  const includeGraph = field(graph, "同时返回证据子图", input("checkbox", "includeGraph"));
  const graphTrustPolicy = element("select");
  graphTrustPolicy.name = "graphTrustPolicy";
  for (const [value, label] of [["PUBLISHED_SECONDARY_INCLUSIVE", "权威与已发布次权威事实"], ["AUTHORITATIVE_ONLY", "仅权威事实"]]) {
    const option = element("option", "", label);
    option.value = value;
    graphTrustPolicy.append(option);
  }
  field(graph, "子图事实范围", graphTrustPolicy);
  graph.append(element("p", "muted", "证据子图开关只控制是否随结果返回子图，不关闭检索中的图扩展；事实范围仅约束返回的子图。"));
  includeGraph.addEventListener("change", () => { graphTrustPolicy.disabled = !includeGraph.checked; });
  const budget = element("details", "retrieval-budget-options");
  budget.append(element("summary", "", "高级检索预算"));
  const customLimits = field(budget, "自定义检索预算", input("checkbox", "customLimits"));
  budget.append(element("p", "muted", "默认由服务按当前范围选择预算。自定义值仍受服务上限约束。"));
  const grid = element("div", "form-grid retrieval-limit-grid");
  const limitInputs = {};
  for (const definition of RETRIEVAL_LIMIT_FIELDS) {
    const node = input("number", definition.key);
    node.min = String(definition.min);
    node.max = String(definition.max);
    node.step = String(definition.step);
    node.title = definition.key;
    node.dataset.retrievalLimit = definition.key;
    limitInputs[definition.key] = field(grid, definition.label, node);
  }
  const deduplicate = input("checkbox", "deduplicate_content");
  deduplicate.dataset.retrievalLimit = "deduplicate_content";
  limitInputs.deduplicate_content = field(grid, "去除重复内容", deduplicate);
  budget.append(grid);
  function fillLimits(defaults = {}) {
    for (const definition of RETRIEVAL_LIMIT_FIELDS) {
      limitInputs[definition.key].value = String(defaults[definition.key] ?? definition.fallback);
    }
    deduplicate.checked = defaults.deduplicate_content ?? true;
  }
  function syncBudget() {
    for (const node of Object.values(limitInputs)) node.disabled = !customLimits.checked;
  }
  customLimits.addEventListener("change", syncBudget);
  const presets = element("div", "retrieval-budget-presets");
  function usePreset(defaults) {
    fillLimits(defaults);
    customLimits.checked = true;
    syncBudget();
    changed();
  }
  presets.append(button("载入通用预算", () => usePreset(bootstrap.defaults?.retrieval_limits)));
  if (bootstrap.industrial?.retrieval_limits) {
    presets.append(button("载入工业预算", () => usePreset(bootstrap.industrial.retrieval_limits)));
  }
  budget.append(presets);
  function reset(notify = true) {
    documentIds.value = versionIds.value = publishedBefore.value = "";
    includeGraph.checked = false;
    graphTrustPolicy.value = "PUBLISHED_SECONDARY_INCLUSIVE";
    graphTrustPolicy.disabled = true;
    customLimits.checked = false;
    budget.open = false;
    root.open = false;
    fillLimits(bootstrap.defaults?.retrieval_limits);
    syncBudget();
    for (const host of diagnosticsHosts) host.replaceChildren();
    diagnosticsHosts.clear();
    if (notify) onChange();
  }
  const sourceFilters = element("details", "retrieval-source-options");
  sourceFilters.append(element("summary", "", "限定文档与版本（可选）"),
    element("p", "muted", "也可从「来源资料」选择文档后进入检索。以下 ID 过滤用于精确指定范围，留空则不增加此项限制。"), filters);
  body.append(graph, sourceFilters, budget, button("恢复默认检索选项", () => reset()));
  root.append(body);
  container.replaceChildren(root);
  reset(false);
  return {
    requestOptions() {
      return parseRetrievalOptions({
        documentIds: documentIds.value,
        versionIds: versionIds.value,
        publishedBefore: publishedBefore.value,
        includeGraph: includeGraph.checked,
        graphTrustPolicy: graphTrustPolicy.value,
        customLimits: customLimits.checked,
        limits: Object.fromEntries(Object.entries(limitInputs).map(([key, node]) =>
          [key, node.type === "checkbox" ? node.checked : node.value])),
      });
    },
    reset,
    renderDiagnostics(result, host) {
      host.replaceChildren();
      diagnosticsHosts.add(host);
      if (!result) return;
      renderProcess(result, host);
    },
  };
}
